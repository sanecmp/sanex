"""Shared low-level helpers for the system D-Bus."""

from collections.abc import Callable
from typing import Protocol, TypeVar

from dbus_fast import Message, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, MessageType
from dbus_fast.errors import DBusFastError

from ..exceptions import DBusReplyError


PropertyT = TypeVar("PropertyT")
DBUS_OPERATION_ERRORS = (
    DBusFastError,
    DBusReplyError,
    OSError,
    EOFError,
    ValueError,
)


class SystemBus(Protocol):
    async def connect(self) -> object: ...

    async def call(self, message: Message) -> Message: ...

    def add_message_handler(self, handler: Callable[[Message], Message | bool | None]) -> None: ...

    def remove_message_handler(self, handler: Callable[[Message], Message | bool | None]) -> None: ...

    def disconnect(self) -> None: ...


def system_bus() -> SystemBus:
    """Create an unconnected system bus."""
    return MessageBus(bus_type=BusType.SYSTEM)


def property_value(
    properties: dict[object, object],
    name: str,
    expected_type: type[PropertyT],
) -> PropertyT:
    """Extract one strictly typed value from a D-Bus GetAll response."""
    variant = properties.get(name)

    if not isinstance(variant, Variant):
        raise ValueError(f"property {name} is missing or invalid")

    value = variant.value

    if not isinstance(value, expected_type) or expected_type is int and isinstance(value, bool):
        raise ValueError(f"property {name} has an invalid type")

    return value


def raise_dbus_error(reply: Message) -> None:
    """Raise a regular exception for a D-Bus error reply."""

    if reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply.body else reply.error_name or "unknown D-Bus error"
        raise DBusReplyError(f"{reply.error_name or "D-Bus error"}: {detail}")
