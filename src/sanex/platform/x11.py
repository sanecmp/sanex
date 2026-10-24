"""Supplemental EWMH windows on the session's local X server or Xwayland."""

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from Xlib import X, Xatom, display, error
from Xlib.protocol.event import ClientMessage
from sanelib.protocol import ObservationSource

from ..exceptions import X11Error
from ..model.window import AtspiWindow, AtspiWindowReference, AtspiWindowSnapshot, WindowRole
from ..utils.resources import close_resources


_LOCAL_DISPLAY = re.compile(r"(?:unix)?:[0-9]+(?:\.[0-9]+)?")
_X11_ERRORS = (error.XError, error.DisplayError, error.ConnectionClosedError, OSError)


def open_display(name: str, authority: str) -> Any:
    """Open as the session EUID; never inherit root's Xauthority path."""
    previous = os.environ.get("XAUTHORITY")
    os.environ["XAUTHORITY"] = authority

    try:
        return display.Display(name)

    finally:

        if previous is None:
            os.environ.pop("XAUTHORITY", None)

        else:
            os.environ["XAUTHORITY"] = previous


@dataclass(slots=True)
class X11Client:
    """Read bounded EWMH properties and request ordinary window closure."""

    display_factory: Callable[[str, str], Any] = open_display
    _display: Any = field(default=None, init=False, repr=False)
    _atoms: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def connect(self, environment: Mapping[str, str], home: Path) -> None:
        """Optionally connect to a local display, only while using the user's EUID."""
        name = environment.get("DISPLAY", "")

        if not name or self._display is not None:
            return

        if not _LOCAL_DISPLAY.fullmatch(name):
            raise X11Error("only local X11 displays are supported")

        authority = environment.get("XAUTHORITY") or f"{home / '.Xauthority'}"

        if not Path(authority).is_absolute() or any(ord(character) < 32 for character in authority):
            raise X11Error("invalid Xauthority path")

        connection = None

        try:
            connection = self.display_factory(name, authority)
            atoms = {atom: connection.intern_atom(atom) for atom in (
                "_NET_CLIENT_LIST", "_NET_WM_PID", "_NET_WM_NAME", "UTF8_STRING",
                "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_DOCK", "_NET_CLOSE_WINDOW",
            )}

        except BaseException as failure:

            if connection is not None:
                close_resources(("close incomplete X11 connection", connection.close))

            if isinstance(failure, _X11_ERRORS):
                raise X11Error(f"X11 connection failed ({type(failure).__name__})") from failure

            raise

        self._display = connection
        self._atoms = atoms

    def close(self) -> None:
        """Release the X11 connection, including after an incomplete setup."""
        connection = self._display
        self._display = None
        self._atoms.clear()

        if connection is not None:

            try:
                connection.close()

            except _X11_ERRORS as failure:
                raise X11Error(f"X11 close failed ({type(failure).__name__})") from failure

    def read_windows(self) -> AtspiWindowSnapshot:
        """Return EWMH clients; absent DISPLAY is an intentionally empty source."""
        connection = self._display

        if connection is None:
            return AtspiWindowSnapshot()

        windows = []
        unavailable = []

        try:
            root = connection.screen().root
            clients = root.get_property(self._atoms["_NET_CLIENT_LIST"], Xatom.WINDOW, 0, 16_384)

            if clients is None:
                return AtspiWindowSnapshot()

            if clients.format != 32 or clients.bytes_after:
                raise X11Error("EWMH client list is invalid or too large")

            for ident in sorted(set(clients.value)):

                try:
                    window = self._read_window(ident)

                except error.XError:
                    unavailable.append(AtspiWindowReference(bus="ewmh", path=f"{ident}"))
                    continue

                if window is not None:
                    windows.append(window)

        except _X11_ERRORS as failure:
            raise X11Error(f"EWMH snapshot failed ({type(failure).__name__})") from failure

        return AtspiWindowSnapshot(windows=tuple(windows), unavailable_windows=tuple(unavailable))

    def close_window(self, window: AtspiWindow) -> bool:
        """Ask the window manager to close a still-matching client, never kill its PID."""

        if window.source != ObservationSource.EWMH or self._display is None:
            return False

        try:
            ident = int(window.path)
            current = self._read_window(ident)

            if current is None or current.pid != window.pid:
                return False

            request = ClientMessage(window=ident, client_type=self._atoms["_NET_CLOSE_WINDOW"], data=(32, [X.CurrentTime, 2, 0, 0, 0]))
            self._display.screen().root.send_event(request, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            self._display.flush()
            return True

        except (ValueError, *_X11_ERRORS) as failure:
            raise X11Error(f"EWMH close failed ({type(failure).__name__})") from failure

    def _read_window(self, ident: int) -> AtspiWindow | None:
        window = self._display.create_resource_object("window", ident)
        kind = window.get_property(self._atoms["_NET_WM_WINDOW_TYPE"], Xatom.ATOM, 0, 32)
        excluded = {self._atoms["_NET_WM_WINDOW_TYPE_DESKTOP"], self._atoms["_NET_WM_WINDOW_TYPE_DOCK"]}

        if kind is not None and any(value in excluded for value in kind.value):
            return None

        owner = window.get_property(self._atoms["_NET_WM_PID"], Xatom.CARDINAL, 0, 1)

        if owner is None or owner.format != 32 or len(owner.value) != 1 or owner.value[0] <= 0:
            return None

        title = window.get_property(self._atoms["_NET_WM_NAME"], self._atoms["UTF8_STRING"], 0, 128)

        if title is None:
            title = window.get_property(Xatom.WM_NAME, Xatom.STRING, 0, 128)

        name = ""

        if title is not None and title.format == 8:
            name = bytes(title.value).decode("utf-8", errors="replace")

        return AtspiWindow(bus="ewmh", path=f"{ident}", pid=int(owner.value[0]), title=name,
                           role=WindowRole.WINDOW, source=ObservationSource.EWMH)
