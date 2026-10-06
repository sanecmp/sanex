"""Explicit resource and privilege lifecycle for the per-session AT-SPI agent."""

import asyncio
import ctypes
import os
import socket

from ..exceptions import WindowAgentError
from ..platform.atspi import AtspiClient, addressed_bus
from ..platform.window_agent import WindowAgentServer
from ..utils.resources import close_async_resources, close_resources


_PR_SET_DUMPABLE = 4


async def run_agent(fd: int, uid: int, sess_ident: str) -> None:
    """Connect as the session user, restore root EUID and serve requests."""
    _validate_identity(fd, uid, sess_ident)
    channel = socket.socket(fileno=fd)
    writer = None
    client = None

    try:
        _disable_dumping()
        channel.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=channel)
        user_bus_address = f"unix:path=/run/user/{uid}/bus"
        client = AtspiClient(session_bus_factory=lambda: addressed_bus(user_bus_address))

        try:
            os.seteuid(uid)
            await client.connect()

        finally:
            close_resources(("restore root EUID", lambda: os.seteuid(0)))

        await WindowAgentServer(sess_ident, client, reader, writer).serve()

    finally:
        operations = []

        if client is not None:
            operations.append(("close accessibility client", client.close))

        if writer is None:
            operations.append(("close inherited socket", channel.close))

        else:
            operations.extend((
                ("close inherited stream", writer.close),
                ("wait for inherited stream", writer.wait_closed),
            ))

        await close_async_resources(*operations)


def _validate_identity(fd: int, uid: int, sess_ident: str) -> None:

    if os.getresuid() != (0, 0, 0):
        raise WindowAgentError("start", "agent must start with all UIDs equal to root")

    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
        raise WindowAgentError("start", "inherited socket FD is invalid")

    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise WindowAgentError("start", "session UID is invalid")

    if not sess_ident:
        raise WindowAgentError("start", "session ident is empty")


def _disable_dumping() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(
        _PR_SET_DUMPABLE,
        0,
        0,
        0,
        0,
    )

    if result != 0:
        error_number = ctypes.get_errno()
        raise WindowAgentError(
            "disable dumping",
            OSError(error_number, os.strerror(error_number)),
        )
