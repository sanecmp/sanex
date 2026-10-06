"""Low-level AT-SPI discovery and top-level window actions."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self, TypeVar

from dbus_fast import Message
from dbus_fast.aio import MessageBus

from ..exceptions import AtspiError
from ..utils.resources import close_resources
from ..model.window import (
    AtspiWindow,
    AtspiWindowReference,
    AtspiWindowSnapshot,
    WindowRole,
)
from .dbus import DBUS_OPERATION_ERRORS, SystemBus, property_value, raise_dbus_error


_SESSION_DESTINATION = "org.a11y.Bus"
_SESSION_PATH = "/org/a11y/bus"
_SESSION_INTERFACE = "org.a11y.Bus"
_REGISTRY_DESTINATION = "org.a11y.atspi.Registry"
_ROOT_PATH = "/org/a11y/atspi/accessible/root"
_ACCESSIBLE_INTERFACE = "org.a11y.atspi.Accessible"
_ACTION_INTERFACE = "org.a11y.atspi.Action"
_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
_DBUS_DESTINATION = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_DBUS_INTERFACE = "org.freedesktop.DBus"
_WINDOW_ROLES = frozenset(WindowRole)
_UNSUPPORTED_ACTION_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownMethod",
    }
)

ValueT = TypeVar("ValueT")

logger = logging.getLogger(__name__)


def session_bus() -> SystemBus:
    """Create an unconnected user session bus."""
    return MessageBus()


def addressed_bus(address: str) -> SystemBus:
    """Create an unconnected bus for an explicit address."""
    return MessageBus(bus_address=address)


@dataclass(slots=True)
class AtspiClient:
    """Discover and act on windows through a user's accessibility bus."""

    session_bus_factory: Callable[[], SystemBus] = session_bus
    atspi_bus_factory: Callable[[str], SystemBus] = addressed_bus
    _bus: SystemBus | None = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    async def connect(self) -> None:
        """Discover and connect to the AT-SPI bus."""

        if self._bus is not None:
            return

        discovery_bus = self.session_bus_factory()
        discovery_owned = True
        bus = None

        try:
            await discovery_bus.connect()
            reply = await discovery_bus.call(
                Message(
                    destination=_SESSION_DESTINATION,
                    path=_SESSION_PATH,
                    interface=_SESSION_INTERFACE,
                    member="GetAddress",
                )
            )
            raise_dbus_error(reply)
            address = self._single_value(reply.body, str, "GetAddress")
            bus = self.atspi_bus_factory(address)
            await bus.connect()
            discovery_owned = False
            discovery_bus.disconnect()

        except BaseException as error:
            operations = []

            if discovery_owned:
                operations.append(("disconnect discovery bus", discovery_bus.disconnect))

            if bus is not None:
                operations.append(("disconnect accessibility bus", bus.disconnect))

            close_resources(*operations)

            if isinstance(error, DBUS_OPERATION_ERRORS):
                raise AtspiError("connect", error) from error

            raise

        self._bus = bus

    def close(self) -> None:
        """Disconnect from the accessibility bus."""

        if self._bus is None:
            return

        bus = self._bus
        self._bus = None
        try:
            bus.disconnect()

        except DBUS_OPERATION_ERRORS as error:
            raise AtspiError("disconnect", error) from error

    async def windows(self) -> AtspiWindowSnapshot:
        """Return top-level windows and mark each inaccessible snapshot part."""
        bus = self._connected_bus()
        try:
            applications = await self._children(bus, _REGISTRY_DESTINATION, _ROOT_PATH)

        except DBUS_OPERATION_ERRORS as error:
            raise AtspiError("list applications", error) from error

        pids: dict[str, int] = {}
        windows: list[AtspiWindow] = []
        unavailable_buses: set[str] = set()
        unavailable_windows: set[tuple[str, str]] = set()

        for app_bus, app_path in applications:
            try:
                children = await self._children(bus, app_bus, app_path)

            except DBUS_OPERATION_ERRORS as error:
                logger.debug("Unable to inspect AT-SPI application %s: %s", app_bus, error)
                unavailable_buses.add(app_bus)
                continue

            for window_bus, window_path in children:
                try:
                    window = await self._read_window(bus, window_bus, window_path, pids)

                except DBUS_OPERATION_ERRORS as error:
                    logger.debug(
                        "Unable to inspect AT-SPI object %s%s: %s",
                        window_bus,
                        window_path,
                        error,
                    )
                    unavailable_windows.add((window_bus, window_path))
                    continue

                if window is not None:
                    windows.append(window)

        return AtspiWindowSnapshot(
            windows=tuple(sorted(windows, key=lambda window: (window.bus, window.path))),
            unavailable_buses=tuple(sorted(unavailable_buses)),
            unavailable_windows=tuple(
                AtspiWindowReference(bus=window_bus, path=window_path)
                for window_bus, window_path in sorted(unavailable_windows)
            ),
        )

    async def close_window(self, window: AtspiWindow) -> bool:
        """Invoke a generic close action when the window exposes one."""
        bus = self._connected_bus()
        try:
            reply = await bus.call(
                Message(
                    destination=window.bus,
                    path=window.path,
                    interface=_ACTION_INTERFACE,
                    member="GetActions",
                )
            )

            if reply.error_name in _UNSUPPORTED_ACTION_ERRORS:
                return False

            raise_dbus_error(reply)
            actions = self._action_names(reply.body)
            action_index = next(
                (
                    index
                    for index, name in enumerate(actions)
                    if name.casefold() == "close" or name.casefold().endswith(".close")
                ),
                None,
            )

            if action_index is None:
                return False

            reply = await bus.call(
                Message(
                    destination=window.bus,
                    path=window.path,
                    interface=_ACTION_INTERFACE,
                    member="DoAction",
                    signature="i",
                    body=[action_index],
                )
            )
            raise_dbus_error(reply)

            if not self._single_value(reply.body, bool, "DoAction"):
                raise ValueError("DoAction rejected the close request")

            return True

        except DBUS_OPERATION_ERRORS as error:
            raise AtspiError(f"close window {window.bus}{window.path}", error) from error

    async def _read_window(
        self,
        bus: SystemBus,
        destination: str,
        path: str,
        pids: dict[str, int],
    ) -> AtspiWindow | None:
        reply = await bus.call(
            Message(
                destination=destination,
                path=path,
                interface=_ACCESSIBLE_INTERFACE,
                member="GetRole",
            )
        )
        raise_dbus_error(reply)
        role_value = self._single_value(reply.body, int, "GetRole")
        try:
            role = WindowRole(role_value)

        except ValueError:
            return None

        if role not in _WINDOW_ROLES:
            return None

        reply = await bus.call(
            Message(
                destination=destination,
                path=path,
                interface=_PROPERTIES_INTERFACE,
                member="GetAll",
                signature="s",
                body=[_ACCESSIBLE_INTERFACE],
            )
        )
        raise_dbus_error(reply)
        properties = self._single_value(reply.body, dict, "GetAll")
        title = property_value(properties, "Name", str)
        pid = pids.get(destination)

        if pid is None:
            pid = await self._process_id(bus, destination)
            pids[destination] = pid

        return AtspiWindow(bus=destination, path=path, pid=pid, title=title, role=role)

    async def _process_id(self, bus: SystemBus, destination: str) -> int:
        reply = await bus.call(
            Message(
                destination=_DBUS_DESTINATION,
                path=_DBUS_PATH,
                interface=_DBUS_INTERFACE,
                member="GetConnectionUnixProcessID",
                signature="s",
                body=[destination],
            )
        )
        raise_dbus_error(reply)
        pid = self._single_value(reply.body, int, "GetConnectionUnixProcessID")

        if isinstance(pid, bool) or pid <= 0:
            raise ValueError("GetConnectionUnixProcessID returned an invalid PID")

        return pid

    async def _children(
        self,
        bus: SystemBus,
        destination: str,
        path: str,
    ) -> tuple[tuple[str, str], ...]:
        reply = await bus.call(
            Message(
                destination=destination,
                path=path,
                interface=_ACCESSIBLE_INTERFACE,
                member="GetChildren",
            )
        )
        raise_dbus_error(reply)
        references = self._single_value(reply.body, list, "GetChildren")

        if not all(
            isinstance(reference, list | tuple)
            and len(reference) == 2
            and isinstance(reference[0], str)
            and reference[0]
            and isinstance(reference[1], str)
            and reference[1]
            for reference in references
        ):
            raise ValueError("GetChildren returned an invalid object reference")

        return tuple((reference[0], reference[1]) for reference in references)

    @staticmethod
    def _single_value(
        body: list[object],
        expected_type: type[ValueT],
        operation: str,
    ) -> ValueT:

        if len(body) != 1 or not isinstance(body[0], expected_type):
            raise ValueError(f"{operation} returned an invalid body")

        return body[0]

    @staticmethod
    def _action_names(body: list[object]) -> tuple[str, ...]:
        actions = AtspiClient._single_value(body, list, "GetActions")

        if not all(
            isinstance(action, list | tuple)
            and len(action) == 3
            and all(isinstance(value, str) for value in action)
            for action in actions
        ):
            raise ValueError("GetActions returned an invalid action")

        return tuple(action[0] for action in actions)

    def _connected_bus(self) -> SystemBus:

        if self._bus is None:
            raise AtspiError("access", RuntimeError("client is not connected"))

        return self._bus
