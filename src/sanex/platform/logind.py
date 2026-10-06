"""Systemd-logind access for graphical session snapshots and termination."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from dbus_fast import Message, Variant
from dbus_fast.constants import MessageType

from ..exceptions import LogindError
from ..model.session import LoginSession, SessionState, SessionType
from ..utils.resources import close_resources
from .dbus import (
    DBUS_OPERATION_ERRORS,
    SystemBus,
    property_value,
    raise_dbus_error,
    system_bus,
)


_DESTINATION = "org.freedesktop.login1"
_MANAGER_PATH = "/org/freedesktop/login1"
_MANAGER_INTERFACE = "org.freedesktop.login1.Manager"
_SESSION_INTERFACE = "org.freedesktop.login1.Session"
_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
_GRAPHICAL_TYPES = frozenset({SessionType.X11.value, SessionType.WAYLAND.value})
_DBUS_DESTINATION = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_DBUS_INTERFACE = "org.freedesktop.DBus"
_SLEEP_MATCH = (
    "type='signal',"
    "interface='org.freedesktop.login1.Manager',"
    "member='PrepareForSleep',"
    "path='/org/freedesktop/login1'"
)


@dataclass(slots=True)
class LogindClient:
    """Maintain one system-bus connection to systemd-logind."""

    bus_factory: Callable[[], SystemBus] = system_bus
    _bus: SystemBus | None = field(default=None, init=False, repr=False)
    _sleep_changes: asyncio.Queue[bool] = field(default_factory=asyncio.Queue, init=False, repr=False)
    _sleep_monitoring: bool = field(default=False, init=False, repr=False)

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
        """Connect to the system bus once."""

        if self._bus is not None:
            return

        bus = self.bus_factory()
        try:
            await bus.connect()

        except BaseException as error:
            close_resources(("disconnect logind bus", bus.disconnect))

            if isinstance(error, DBUS_OPERATION_ERRORS):
                raise LogindError("connect", error) from error

            raise

        self._bus = bus

    def close(self) -> None:
        """Disconnect from the system bus."""

        if self._bus is not None:
            bus = self._bus
            self._bus = None
            monitoring = self._sleep_monitoring
            self._sleep_monitoring = False
            operations = []

            if monitoring:
                operations.append(("remove sleep handler", lambda: bus.remove_message_handler(self._handle_message)))

            operations.append(("disconnect logind bus", bus.disconnect))

            try:
                close_resources(*operations)

            except DBUS_OPERATION_ERRORS as error:
                raise LogindError("disconnect", error) from error

    async def start_sleep_monitoring(self) -> None:
        """Subscribe to logind PrepareForSleep changes."""
        bus = self._connected_bus()

        if self._sleep_monitoring:
            return

        self._sleep_changes = asyncio.Queue()
        self._sleep_monitoring = True
        try:
            bus.add_message_handler(self._handle_message)
            await self._set_sleep_match(bus, "AddMatch")

        except BaseException as error:
            self._sleep_monitoring = False
            close_resources(("remove sleep handler", lambda: bus.remove_message_handler(self._handle_message)))

            if isinstance(error, DBUS_OPERATION_ERRORS):
                raise LogindError("subscribe to sleep changes", error) from error

            raise

    async def stop_sleep_monitoring(self) -> None:
        """Remove the PrepareForSleep subscription."""
        bus = self._connected_bus()

        if not self._sleep_monitoring:
            return

        try:
            await self._set_sleep_match(bus, "RemoveMatch")

        except DBUS_OPERATION_ERRORS as error:
            raise LogindError("unsubscribe from sleep changes", error) from error

        finally:
            self._sleep_monitoring = False
            close_resources(("remove sleep handler", lambda: bus.remove_message_handler(self._handle_message)))

    async def next_sleep_change(self) -> bool:
        """Wait for the next PrepareForSleep state."""

        if not self._sleep_monitoring:
            raise LogindError("wait for sleep change", RuntimeError("sleep monitoring is not active"))

        return await self._sleep_changes.get()

    async def sessions(self) -> tuple[LoginSession, ...]:
        """Return all normal X11 and Wayland login sessions."""
        bus = self._connected_bus()
        try:
            reply = await bus.call(
                Message(
                    destination=_DESTINATION,
                    path=_MANAGER_PATH,
                    interface=_MANAGER_INTERFACE,
                    member="ListSessions",
                )
            )
            raise_dbus_error(reply)
            rows = self._session_rows(reply.body)
            sessions = [await self._read_session(bus, row) for row in rows]
            return tuple(
                sorted(
                    (session for session in sessions if session is not None),
                    key=lambda session: (session.uid, session.ident),
                )
            )

        except LogindError:
            raise

        except DBUS_OPERATION_ERRORS as error:
            raise LogindError("list sessions", error) from error

    async def get_session_ident(self, pid: int) -> str:
        """Return the logind session identity containing one process."""

        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise LogindError("get process session", ValueError("PID is invalid"))

        bus = self._connected_bus()
        try:
            reply = await bus.call(
                Message(
                    destination=_DESTINATION,
                    path=_MANAGER_PATH,
                    interface=_MANAGER_INTERFACE,
                    member="GetSessionByPID",
                    signature="u",
                    body=[pid],
                )
            )
            raise_dbus_error(reply)

            if len(reply.body) != 1 or not isinstance(reply.body[0], str):
                raise ValueError("GetSessionByPID returned an invalid body")

            session_path = reply.body[0]
            reply = await bus.call(
                Message(
                    destination=_DESTINATION,
                    path=session_path,
                    interface=_PROPERTIES_INTERFACE,
                    member="Get",
                    signature="ss",
                    body=[_SESSION_INTERFACE, "Id"],
                )
            )
            raise_dbus_error(reply)

            if len(reply.body) != 1 or not isinstance(reply.body[0], Variant):
                raise ValueError("session Id property is invalid")

            ident = reply.body[0].value

            if not isinstance(ident, str) or not ident:
                raise ValueError("session Id property is invalid")

            return ident

        except LogindError:
            raise

        except DBUS_OPERATION_ERRORS as error:
            raise LogindError(f"get session for PID {pid}", error) from error

    async def terminate(self, ident: str) -> None:
        """Request termination of exactly one logind session."""
        bus = self._connected_bus()
        try:
            reply = await bus.call(
                Message(
                    destination=_DESTINATION,
                    path=_MANAGER_PATH,
                    interface=_MANAGER_INTERFACE,
                    member="TerminateSession",
                    signature="s",
                    body=[ident],
                )
            )
            raise_dbus_error(reply)

        except DBUS_OPERATION_ERRORS as error:
            raise LogindError(f"terminate session {ident}", error) from error

    async def _set_sleep_match(self, bus: SystemBus, member: str) -> None:
        reply = await bus.call(
            Message(
                destination=_DBUS_DESTINATION,
                path=_DBUS_PATH,
                interface=_DBUS_INTERFACE,
                member=member,
                signature="s",
                body=[_SLEEP_MATCH],
            )
        )
        raise_dbus_error(reply)

    def _handle_message(self, message: Message) -> None:

        if (
            message.message_type is MessageType.SIGNAL
            and message.path == _MANAGER_PATH
            and message.interface == _MANAGER_INTERFACE
            and message.member == "PrepareForSleep"
            and message.signature == "b"
            and len(message.body) == 1
            and isinstance(message.body[0], bool)
        ):
            self._sleep_changes.put_nowait(message.body[0])

    def _connected_bus(self) -> SystemBus:

        if self._bus is None:
            raise LogindError("access", RuntimeError("client is not connected"))

        return self._bus

    @staticmethod
    def _session_rows(body: list[object]) -> list[tuple[object, ...]]:

        if len(body) != 1 or not isinstance(body[0], list):
            raise ValueError("ListSessions returned an invalid body")

        rows = body[0]

        if not all(isinstance(row, list | tuple) and len(row) == 5 for row in rows):
            raise ValueError("ListSessions returned an invalid row")

        return [tuple(row) for row in rows]

    @staticmethod
    async def _read_session(bus: SystemBus, row: tuple[object, ...]) -> LoginSession | None:
        listed_ident, listed_uid, listed_login, _seat, path = row

        if (
            not isinstance(listed_ident, str)
            or not listed_ident
            or not isinstance(listed_uid, int)
            or isinstance(listed_uid, bool)
            or not isinstance(listed_login, str)
            or not isinstance(path, str)
            or not path
        ):
            raise ValueError("ListSessions returned invalid session values")

        reply = await bus.call(
            Message(
                destination=_DESTINATION,
                path=path,
                interface=_PROPERTIES_INTERFACE,
                member="GetAll",
                signature="s",
                body=[_SESSION_INTERFACE],
            )
        )
        raise_dbus_error(reply)

        if len(reply.body) != 1 or not isinstance(reply.body[0], dict):
            raise ValueError(f"GetAll returned an invalid body for {path}")

        properties = reply.body[0]
        ident = property_value(properties, "Id", str)
        user = property_value(properties, "User", tuple)
        login = property_value(properties, "Name", str)
        timestamp_us = property_value(properties, "Timestamp", int)
        session_type = property_value(properties, "Type", str)
        session_class = property_value(properties, "Class", str)
        active = property_value(properties, "Active", bool)
        state = property_value(properties, "State", str)
        idle = property_value(properties, "IdleHint", bool)
        locked = property_value(properties, "LockedHint", bool)

        if len(user) != 2 or not isinstance(user[0], int) or isinstance(user[0], bool):
            raise ValueError(f"session {ident} has an invalid User property")

        uid = user[0]

        if ident != listed_ident or uid != listed_uid or login != listed_login:
            raise ValueError(f"session {listed_ident} properties do not match ListSessions")

        if session_class != "user" or session_type not in _GRAPHICAL_TYPES:
            return None

        return LoginSession(
            ident=ident,
            uid=uid,
            login=login,
            path=path,
            started=timestamp_us // 1_000_000,
            type=SessionType(session_type),
            state=SessionState(state),
            active=active,
            idle=idle,
            locked=locked,
        )
