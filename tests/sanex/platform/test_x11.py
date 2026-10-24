"""Isolated EWMH reads and close actions with no real display connection."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from Xlib import error
from sanelib.protocol import ObservationSource

from sanex.exceptions import X11Error
from sanex.platform.x11 import X11Client


def create_display() -> SimpleNamespace:
    """Build bounded X property responses for ordinary and desktop windows."""
    atoms = {}
    windows = {}
    root = SimpleNamespace(
        get_property=Mock(return_value=SimpleNamespace(format=32, bytes_after=0, value=[10, 11, 12, 10])), send_event=Mock(),
    )
    connection = SimpleNamespace(
        intern_atom=lambda name: atoms.setdefault(name, len(atoms) + 1), screen=lambda: SimpleNamespace(root=root),
        create_resource_object=lambda kind, ident: windows[ident], close=Mock(), flush=Mock(),
    )

    def read_property(ident: int, atom: int, property_type: int, offset: int, length: int) -> SimpleNamespace | None:
        assert offset == 0 and length <= 16_384

        if atom == atoms["_NET_WM_WINDOW_TYPE"] and ident == 11:
            return SimpleNamespace(value=[atoms["_NET_WM_WINDOW_TYPE_DESKTOP"]])

        if atom == atoms["_NET_WM_PID"] and ident != 12:
            return SimpleNamespace(format=32, value=[200])

        if atom == atoms["_NET_WM_NAME"]:
            return SimpleNamespace(format=8, value=b"Team Fortress 2")

        return None

    for ident in (10, 11, 12):
        windows[ident] = SimpleNamespace(get_property=Mock(side_effect=lambda *args, ident=ident: read_property(ident, *args)))

    return SimpleNamespace(connection=connection, root=root, windows=windows, atoms=atoms)


def test_ewmh_reads_pid_and_title_ignores_desktop_and_closes_only_the_same_owner() -> None:
    display = create_display()
    client = X11Client(display_factory=lambda name, authority: display.connection)
    client.connect({"DISPLAY": ":1", "XAUTHORITY": "/synthetic/authority"}, Path("/synthetic/home"))

    snapshot = client.read_windows()

    assert snapshot.complete
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert (window.pid, window.title, window.source) == (200, "Team Fortress 2", ObservationSource.EWMH)
    assert client.close_window(window)
    assert display.root.send_event.call_count == 1
    assert display.root.send_event.call_args.args[0].client_type == display.atoms["_NET_CLOSE_WINDOW"]
    assert not client.close_window(window.model_copy(update={"pid": 201}))
    client.close()
    display.connection.close.assert_called_once()


def test_missing_display_never_opens_a_connection() -> None:
    factory = Mock()
    client = X11Client(factory)
    client.connect({}, Path("/synthetic/home"))

    assert client.read_windows().windows == ()
    factory.assert_not_called()


@pytest.mark.parametrize("name", ["other-host:0", "tcp/host:1", "invalid"])
def test_x11_rejects_remote_displays(name: str) -> None:
    factory = Mock()

    with pytest.raises(X11Error, match="only local"):
        X11Client(factory).connect({"DISPLAY": name}, Path("/synthetic/home"))

    factory.assert_not_called()


def test_failed_atom_setup_releases_the_connection() -> None:
    connection = SimpleNamespace(intern_atom=Mock(side_effect=OSError("unavailable")), close=Mock())

    with pytest.raises(X11Error, match="connection failed"):
        X11Client(lambda name, authority: connection).connect({"DISPLAY": ":1"}, Path("/synthetic/home"))

    connection.close.assert_called_once()


def test_broken_display_does_not_report_a_complete_empty_snapshot() -> None:
    display = create_display()
    client = X11Client(lambda name, authority: display.connection)
    client.connect({"DISPLAY": ":1"}, Path("/synthetic/home"))
    display.root.get_property.side_effect = error.ConnectionClosedError("synthetic")

    with pytest.raises(X11Error, match="snapshot failed"):
        client.read_windows()

    client.close()
