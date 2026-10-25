"""Shared low-level helpers for D-Bus connections."""

import logging
from collections.abc import Callable
from typing import Protocol, TypeVar

from dbus_fast import Message, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, MessageType
from dbus_fast.errors import DBusFastError

from ..exceptions import DBusReplyError
from ..utils.resources import close_async_resources


PropertyT = TypeVar("PropertyT")
logger = logging.getLogger(__name__)
DBUS_OPERATION_ERRORS = (
    DBusFastError,
    DBusReplyError,
    OSError,
    EOFError,
    ValueError,
)


class SystemBus(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> object: ...

    async def call(self, message: Message) -> Message: ...

    def add_message_handler(self, handler: Callable[[Message], Message | bool | None]) -> None: ...

    def remove_message_handler(self, handler: Callable[[Message], Message | bool | None]) -> None: ...

    def disconnect(self) -> None: ...

    async def wait_for_disconnect(self) -> None: ...


def create_system_bus() -> SystemBus:
    """Create an unconnected system bus."""
    return MessageBus(bus_type=BusType.SYSTEM)


async def disconnect_bus(bus: SystemBus, *, wait: bool = True) -> None:
    """Close a bus and retrieve its completion after a successful connection."""
    async def finish_disconnect() -> None:

        if wait and not bus.connected:
            try:
                await bus.wait_for_disconnect()

            except (EOFError, OSError) as error:
                # A peer may already be gone during logout; still retrieve its disconnect exception.
                logger.debug("D-Bus connection was already closed (%s)", type(error).__name__)

    await close_async_resources(
        ("disconnect D-Bus", bus.disconnect), ("wait for D-Bus disconnect", finish_disconnect),
    )


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
