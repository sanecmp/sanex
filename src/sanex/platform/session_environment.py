"""Read only display connection hints from the session's user manager."""

from dbus_fast import Message, Variant

from ..exceptions import WindowAgentError
from .atspi import create_addressed_bus
from .dbus import DBUS_OPERATION_ERRORS, raise_dbus_error


async def read_session_environment(address: str) -> dict[str, str]:
    """Read DISPLAY and XAUTHORITY without importing arbitrary session variables."""
    bus = create_addressed_bus(address)

    try:
        await bus.connect()
        reply = await bus.call(Message(
            destination="org.freedesktop.systemd1", path="/org/freedesktop/systemd1",
            interface="org.freedesktop.DBus.Properties", member="Get", signature="ss",
            body=["org.freedesktop.systemd1.Manager", "Environment"],
        ))
        raise_dbus_error(reply)

        if len(reply.body) != 1 or not isinstance(reply.body[0], Variant) or reply.body[0].signature != "as":
            raise ValueError("invalid user-manager environment")

        environment = {}

        for entry in reply.body[0].value:

            if not isinstance(entry, str):
                raise ValueError("invalid user-manager environment entry")

            key, separator, value = entry.partition("=")

            if separator and key in {"DISPLAY", "XAUTHORITY"} and len(value) <= 4_096:
                environment[key] = value

        return environment

    except DBUS_OPERATION_ERRORS as failure:
        raise WindowAgentError("read display environment", type(failure).__name__) from failure

    finally:
        bus.disconnect()
