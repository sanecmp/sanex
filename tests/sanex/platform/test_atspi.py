"""Tests for AT-SPI window discovery and close actions."""

from typing import Any
from unittest.mock import Mock

import pytest
from dbus_fast import Variant
from dbus_fast.auth import AuthExternal, UID_NOT_SPECIFIED

from sanex.exceptions import AtspiError
from sanex.model.window import AtspiWindow, AtspiWindowReference, WindowRole
from sanex.platform import atspi as atspi_module
from sanex.platform.atspi import AtspiClient
from tests.sanex.platform.fakes import FakeBus, dbus_error, dbus_reply

import asyncio


def window_properties(sample: dict[str, Any]) -> dict[str, Variant]:
    """Build accessible property variants for one fixture window."""
    return {"Name": Variant("s", sample["title"])}


def test_addressed_bus_authenticates_with_unix_peer_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    create_bus = Mock()
    monkeypatch.setattr(atspi_module, "MessageBus", create_bus)

    bus = atspi_module.create_addressed_bus("unix:path=/test/user-bus")

    create_bus.assert_called_once()
    options = create_bus.call_args.kwargs
    assert options["bus_address"] == "unix:path=/test/user-bus"
    authentication = options["auth"]
    assert isinstance(authentication, AuthExternal)
    assert authentication.uid == UID_NOT_SPECIFIED
    assert bus is create_bus.return_value


@pytest.mark.asyncio
async def test_discovers_top_level_windows_and_reuses_bus_pid(
    atspi_payload: dict[str, Any],
) -> None:
    app = atspi_payload["application"]
    children = atspi_payload["children"]
    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    atspi_bus = FakeBus(
        [
            dbus_reply([[[app["bus"], app["path"]]]]),
            dbus_reply([[[entry["bus"], entry["path"]] for entry in children]]),
            dbus_reply([children[0]["role"]]),
            dbus_reply([window_properties(children[0])]),
            dbus_reply([app["pid"]]),
            dbus_reply([children[1]["role"]]),
            dbus_reply([window_properties(children[1])]),
            dbus_reply([children[2]["role"]]),
        ]
    )
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: atspi_bus,
    )
    await client.connect()

    snapshot = await client.read_windows()

    assert snapshot.windows == (
        AtspiWindow(
            bus=children[0]["bus"],
            path=children[0]["path"],
            pid=children[0]["pid"],
            title=children[0]["title"],
            role=WindowRole.FRAME,
        ),
        AtspiWindow(
            bus=children[1]["bus"],
            path=children[1]["path"],
            pid=children[1]["pid"],
            title=children[1]["title"],
            role=WindowRole.DIALOG,
        ),
    )
    assert snapshot.complete
    assert [message.member for message in discovery_bus.messages] == ["GetAddress"]
    assert [message.member for message in atspi_bus.messages].count("GetConnectionUnixProcessID") == 1
    assert not discovery_bus.connected
    await client.close()
    assert not atspi_bus.connected


@pytest.mark.asyncio
async def test_marks_window_that_disappears_during_snapshot_as_unavailable(
    atspi_payload: dict[str, Any],
) -> None:
    app = atspi_payload["application"]
    window = atspi_payload["children"][0]
    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    atspi_bus = FakeBus(
        [
            dbus_reply([[[app["bus"], app["path"]]]]),
            dbus_reply([[[window["bus"], window["path"]]]]),
            dbus_error(
                "org.freedesktop.DBus.Error.UnknownObject",
                "window disappeared",
            ),
        ]
    )
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: atspi_bus,
    )
    await client.connect()

    snapshot = await client.read_windows()

    assert snapshot.windows == ()
    assert snapshot.unavailable_windows == (
        AtspiWindowReference(bus=window["bus"], path=window["path"]),
    )
    assert not snapshot.complete


@pytest.mark.asyncio
async def test_close_window_invokes_exposed_close_action(atspi_payload: dict[str, Any]) -> None:
    sample = atspi_payload["children"][0]
    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    atspi_bus = FakeBus(
        [
            dbus_reply([[
                ["doDefault", "", ""],
                ["window.close", "", "<Alt>F4"],
            ]]),
            dbus_reply([True]),
        ]
    )
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: atspi_bus,
    )
    await client.connect()
    window = AtspiWindow(
        bus=sample["bus"],
        path=sample["path"],
        pid=sample["pid"],
        title=sample["title"],
        role=WindowRole.FRAME,
    )

    assert await client.close_window(window)
    action = atspi_bus.messages[1]
    assert action.member == "DoAction"
    assert action.body == [1]


@pytest.mark.asyncio
async def test_close_window_returns_false_without_close_action(
    atspi_payload: dict[str, Any],
) -> None:
    sample = atspi_payload["children"][0]
    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    atspi_bus = FakeBus([dbus_reply([[
        ["doDefault", "", ""],
    ]])])
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: atspi_bus,
    )
    await client.connect()
    window = AtspiWindow(
        bus=sample["bus"],
        path=sample["path"],
        pid=sample["pid"],
        title=sample["title"],
        role=WindowRole.FRAME,
    )

    assert not await client.close_window(window)
    assert [message.member for message in atspi_bus.messages] == ["GetActions"]


@pytest.mark.asyncio
async def test_close_window_returns_false_without_action_interface(
    atspi_payload: dict[str, Any],
) -> None:
    sample = atspi_payload["children"][0]
    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    atspi_bus = FakeBus(
        [
            dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                "GetActions is unavailable",
            )
        ]
    )
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: atspi_bus,
    )
    await client.connect()
    window = AtspiWindow(
        bus=sample["bus"],
        path=sample["path"],
        pid=sample["pid"],
        title=sample["title"],
        role=WindowRole.FRAME,
    )

    assert not await client.close_window(window)


@pytest.mark.asyncio
async def test_client_requires_explicit_connection() -> None:
    client = AtspiClient(
        session_bus_factory=lambda: FakeBus([]),
        atspi_bus_factory=lambda address: FakeBus([]),
    )

    with pytest.raises(AtspiError, match="client is not connected"):
        await client.read_windows()


@pytest.mark.asyncio
async def test_snapshot_does_not_wrap_unexpected_implementation_failure(
    atspi_payload: dict[str, Any],
) -> None:
    class BrokenBus(FakeBus):
        async def call(self, message: object) -> object:
            raise RuntimeError("implementation bug")

    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: BrokenBus([]),
    )
    await client.connect()

    with pytest.raises(RuntimeError, match="implementation bug"):
        await client.read_windows()


@pytest.mark.asyncio
async def test_close_window_raises_when_action_rejects_request(
    atspi_payload: dict[str, Any],
) -> None:
    sample = atspi_payload["children"][0]
    discovery_bus = FakeBus([dbus_reply([atspi_payload["address"]])])
    atspi_bus = FakeBus(
        [
            dbus_reply([[["close", "", ""]]]),
            dbus_reply([False]),
        ]
    )
    client = AtspiClient(
        session_bus_factory=lambda: discovery_bus,
        atspi_bus_factory=lambda address: atspi_bus,
    )
    await client.connect()
    window = AtspiWindow(
        bus=sample["bus"],
        path=sample["path"],
        pid=sample["pid"],
        title=sample["title"],
        role=WindowRole.FRAME,
    )

    with pytest.raises(AtspiError, match="rejected the close request"):
        await client.close_window(window)


@pytest.mark.parametrize("step", ["discovery_connect", "GetAddress", "factory", "atspi_connect", "discovery_disconnect"])
@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_partial_connect_releases_both_buses(step: str, cancel: bool) -> None:

    failure = asyncio.CancelledError("connection interrupted") if cancel else OSError("connection interrupted")
    discovery = FakeBus([dbus_reply(["unix:path=/test/accessibility"])])
    accessibility = FakeBus([])

    def create_accessibility(address: str) -> FakeBus:
        assert address == "unix:path=/test/accessibility"

        if step == "factory":
            raise failure

        return accessibility

    failures = {
        "discovery_connect": (discovery, "connect"),
        "GetAddress": (discovery, "GetAddress"),
        "atspi_connect": (accessibility, "connect"),
        "discovery_disconnect": (discovery, "disconnect"),
    }

    if step in failures:
        bus, operation = failures[step]
        bus.failures[operation] = failure

    client = AtspiClient(session_bus_factory=lambda: discovery, atspi_bus_factory=create_accessibility)
    expected = asyncio.CancelledError if cancel else AtspiError

    with pytest.raises(expected, match="connection interrupted"):
        await client.connect()

    assert discovery.disconnect_count == 1
    assert accessibility.disconnect_count == (1 if step in {"atspi_connect", "discovery_disconnect"} else 0)

    with pytest.raises(AtspiError, match="client is not connected"):
        await client.read_windows()


@pytest.mark.asyncio
async def test_connect_preserves_primary_failure_and_logs_cleanup_error(caplog: pytest.LogCaptureFixture) -> None:
    discovery = FakeBus([], {"GetAddress": OSError("primary failure"), "disconnect": OSError("cleanup failure")})
    client = AtspiClient(session_bus_factory=lambda: discovery)

    with pytest.raises(AtspiError, match="primary failure"):
        await client.connect()

    assert "cleanup failure" in caplog.text
    assert discovery.disconnect_count == 1


@pytest.mark.asyncio
async def test_reconnects_after_idle_bus_disconnect_and_retrieves_its_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    discovery_buses = [FakeBus([dbus_reply(["unix:path=/test/atspi"])]) for _unused in range(2)]
    accessibility_buses = [FakeBus([dbus_reply([[]])]) for _unused in range(2)]
    discoveries = iter(discovery_buses)
    connections = iter(accessibility_buses)
    client = AtspiClient(lambda: next(discoveries), lambda address: next(connections))

    with caplog.at_level("INFO"):
        await client.connect()
        assert (await client.read_windows()).complete
        accessibility_buses[0].drop(EOFError("synthetic disconnect"))

        await client.connect()
        assert (await client.read_windows()).complete
        await client.close()
        await client.close()

    assert [bus.disconnect_wait_count for bus in discovery_buses] == [1, 1]
    assert [bus.disconnect_wait_count for bus in accessibility_buses] == [1, 1]
    assert [bus.disconnect_count for bus in accessibility_buses] == [1, 1]
    assert "AT-SPI bus disconnected; reconnecting" in caplog.text
    assert "AT-SPI connection restored" in caplog.text
    assert "synthetic disconnect" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_retrieves_an_idle_disconnect_without_unhandled_future() -> None:
    discovery = FakeBus([dbus_reply(["unix:path=/test/atspi"])])
    accessibility = FakeBus([])
    client = AtspiClient(lambda: discovery, lambda address: accessibility)
    await client.connect()
    accessibility.drop(EOFError("synthetic logout"))

    await client.close()

    assert accessibility.disconnect_wait_count == 1
    assert not accessibility.connected


@pytest.mark.asyncio
async def test_failed_reconnect_can_be_retried_without_reusing_the_dead_bus() -> None:
    discovery = FakeBus([dbus_reply(["unix:path=/test/atspi"]), dbus_reply(["unix:path=/test/atspi"])])
    accessibility = FakeBus([])
    replacement = FakeBus([dbus_reply([[]])])
    connections = iter((accessibility, replacement))
    client = AtspiClient(lambda: discovery, lambda address: next(connections))
    await client.connect()
    accessibility.drop(EOFError())
    discovery.failures["connect"] = OSError("synthetic unavailable bus")

    with pytest.raises(AtspiError, match="synthetic unavailable bus"):
        await client.connect()

    discovery.failures.clear()
    await client.connect()
    assert (await client.read_windows()).complete
    await client.close()
    assert accessibility.disconnect_wait_count == 1
    assert replacement.disconnect_wait_count == 1
