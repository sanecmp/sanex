"""Tests for the private root-to-window-agent transport."""

import asyncio
from collections.abc import Callable

import pytest

from tests.sanex.resource_fakes import MemoryWriter

from sanex.exceptions import WindowAgentError
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.window_agent import (
    WindowAgentClient,
    WindowAgentPool,
    WindowAgentServer,
)


class FakeBackend:
    def __init__(self, windows: tuple[AtspiWindow, ...]) -> None:
        self.snapshot = AtspiWindowSnapshot(windows=windows)
        self.closed: list[AtspiWindow] = []

    async def windows(self) -> AtspiWindowSnapshot:
        return self.snapshot

    async def close_window(self, window: AtspiWindow) -> bool:
        self.closed.append(window)
        return True


def login_session(uid: int = 1001, ident: str = "3") -> LoginSession:
    return LoginSession(
        ident=ident,
        uid=uid,
        login="child",
        path=f"/session/{ident}",
        started=1_790_842_334,
        type=SessionType.WAYLAND,
        state=SessionState.ACTIVE,
        active=True,
        idle=False,
        locked=False,
    )


def sample_window() -> AtspiWindow:
    return AtspiWindow(
        bus=":1.188",
        path="/org/a11y/atspi/accessible/124",
        pid=6443,
        title="Synthetic window",
        role=WindowRole.FRAME,
    )


@pytest.mark.asyncio
async def test_client_and_server_exchange_snapshot_and_close_request(
    stream_pair: Callable[[], tuple[asyncio.StreamReader, MemoryWriter, asyncio.StreamReader, MemoryWriter]],
) -> None:
    root_reader, root_writer, agent_reader, agent_writer = stream_pair()
    window = sample_window()
    backend = FakeBackend((window,))
    server = WindowAgentServer("3", backend, agent_reader, agent_writer)
    task = asyncio.create_task(server.serve())
    client = WindowAgentClient(1001, "3", root_reader, root_writer)

    snapshot = await client.windows(login_session())
    accepted = await client.close_window(1001, "3", window)
    await client.close()
    await task
    agent_writer.close()
    await agent_writer.wait_closed()

    assert snapshot.windows == (window,)
    assert accepted
    assert backend.closed == [window]


@pytest.mark.asyncio
async def test_pool_rejects_uid_mismatch_without_using_agent_channel(
    stream_pair: Callable[[], tuple[asyncio.StreamReader, MemoryWriter, asyncio.StreamReader, MemoryWriter]],
) -> None:
    root_reader, root_writer, agent_reader, agent_writer = stream_pair()
    client = WindowAgentClient(1001, "3", root_reader, root_writer)
    pool = WindowAgentPool()
    pool.add(client)

    with pytest.raises(WindowAgentError, match="does not match 1002"):
        await pool.windows(login_session(uid=1002))

    await client.close()
    agent_writer.close()
    await agent_writer.wait_closed()
