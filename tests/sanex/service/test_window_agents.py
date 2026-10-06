"""Tests for per-session window-agent process lifecycle management."""

import asyncio
from collections import deque
from collections.abc import Awaitable
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from sanex.exceptions import WindowAgentError
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.window_agent import WindowAgentServer
from sanex.service import window_agents as agent_module
from sanex.service.window_agents import WindowAgentManager
from tests.sanex.resource_fakes import FakeSocket, MemoryWriter

from unittest.mock import AsyncMock
from tests.sanex.resource_fakes import FakeProcess as ResourceProcess
from tests.sanex.resource_fakes import FakeWriter


class FakeBackend:
    def __init__(
        self,
        windows: tuple[AtspiWindow, ...],
        blocked: bool = False,
    ) -> None:
        self.snapshot = AtspiWindowSnapshot(windows=windows)
        self.closed: list[AtspiWindow] = []
        self.release = asyncio.Event()

        if not blocked:
            self.release.set()

    async def windows(self) -> AtspiWindowSnapshot:
        await self.release.wait()
        return self.snapshot

    async def close_window(self, window: AtspiWindow) -> bool:
        self.closed.append(window)
        return True


class FakeProcess:
    def __init__(self, task: asyncio.Task[None]) -> None:
        self.task = task
        self.stderr: asyncio.StreamReader | None = None

    @property
    def returncode(self) -> int | None:

        if not self.task.done():
            return None

        if self.task.cancelled():
            return -15

        return 1 if self.task.exception() is not None else 0

    async def wait(self) -> int:
        try:
            await self.task

        except asyncio.CancelledError:
            return -15

        except Exception:
            return 1

        return 0

    def terminate(self) -> None:
        self.task.cancel()

    def kill(self) -> None:
        self.task.cancel()


class FakeLauncher:
    def __init__(self, window: AtspiWindow, blocked: bool = False) -> None:
        self.window = window
        self.blocked = blocked
        self.streams = deque()
        self.sessions: list[LoginSession] = []
        self.backends: list[FakeBackend] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, session: LoginSession, fd: int) -> FakeProcess:
        root_reader = asyncio.StreamReader()
        reader = asyncio.StreamReader()
        root_writer = MemoryWriter(reader)
        writer = MemoryWriter(root_reader)
        self.streams.append((root_reader, root_writer))
        backend = FakeBackend((self.window,), self.blocked)

        async def run() -> None:
            try:
                await WindowAgentServer(
                    session.ident,
                    backend,
                    reader,
                    writer,
                ).serve()
            finally:
                writer.close()
                await writer.wait_closed()

        process = FakeProcess(asyncio.create_task(run()))
        self.sessions.append(session)
        self.backends.append(backend)
        self.processes.append(process)
        return process


def login_session(sample: dict[str, Any]) -> LoginSession:
    """Convert one graphical session datafixture entry."""
    return LoginSession(
        ident=sample["ident"],
        uid=sample["uid"],
        login=sample["login"],
        path=sample["path"],
        started=sample["timestamp_us"] // 1_000_000,
        type=SessionType(sample["type"]),
        state=SessionState(sample["state"]),
        active=sample["active"],
        idle=sample["idle"],
        locked=sample["locked"],
    )


def atspi_window(payload: dict[str, Any]) -> AtspiWindow:
    """Convert the first top-level window datafixture entry."""
    sample = payload["children"][0]
    return AtspiWindow(
        bus=sample["bus"],
        path=sample["path"],
        pid=sample["pid"],
        title=sample["title"],
        role=WindowRole(sample["role"]),
    )


def manager(launcher: FakeLauncher, timeout: float = 5.0) -> WindowAgentManager:
    """Build a manager around the in-process agent launcher."""
    return WindowAgentManager(
        executable=Path("/opt/sanex/bin/sanex-window-agent"),
        request_timeout=timeout,
        shutdown_timeout=2.0,
        launcher=launcher,
    )


@pytest.fixture
def manager_channels(monkeypatch: pytest.MonkeyPatch) -> list[FakeLauncher]:
    launchers = []
    original_socket = agent_module.socket
    monkeypatch.setattr(agent_module, "socket", SimpleNamespace(
        socketpair=lambda *args: (FakeSocket(7), FakeSocket(8)),
        AF_UNIX=original_socket.AF_UNIX, SOCK_STREAM=original_socket.SOCK_STREAM,
    ))

    async def open_connection(**kwargs: object) -> tuple[asyncio.StreamReader, MemoryWriter]:
        return launchers[-1].streams.popleft()

    monkeypatch.setattr(agent_module.asyncio, "open_connection", open_connection)
    return launchers


@pytest.mark.asyncio
async def test_reconcile_routes_requests_and_stops_removed_agent(
    session_payload: list[dict[str, Any]],
    atspi_payload: dict[str, Any],
    manager_channels: list[FakeLauncher],
) -> None:
    session = login_session(session_payload[0])
    window = atspi_window(atspi_payload)
    launcher = FakeLauncher(window)
    manager_channels.append(launcher)
    agents = manager(launcher)

    await agents.reconcile((session,))

    assert (await agents.windows(session)).windows == (window,)
    assert await agents.close_window(session.uid, session.ident, window)
    assert launcher.backends[0].closed == [window]

    await agents.reconcile(())

    assert agents.pool.clients == {}
    assert launcher.processes[0].returncode == 0


@pytest.mark.asyncio
async def test_reconcile_replaces_agent_when_session_uid_changes(
    session_payload: list[dict[str, Any]],
    atspi_payload: dict[str, Any],
    manager_channels: list[FakeLauncher],
) -> None:
    first = login_session(session_payload[0])
    replacement = first.model_copy(update={"uid": first.uid + 1})
    launcher = FakeLauncher(atspi_window(atspi_payload))
    manager_channels.append(launcher)
    agents = manager(launcher)

    await agents.reconcile((first,))
    await agents.reconcile((replacement,))

    assert [session.uid for session in launcher.sessions] == [first.uid, replacement.uid]
    assert agents.pool.clients[first.ident].uid == replacement.uid
    assert launcher.processes[0].returncode == 0

    await agents.close()


@pytest.mark.asyncio
async def test_reconcile_restarts_exited_agent(
    session_payload: list[dict[str, Any]],
    atspi_payload: dict[str, Any],
    manager_channels: list[FakeLauncher],
) -> None:
    session = login_session(session_payload[0])
    launcher = FakeLauncher(atspi_window(atspi_payload))
    manager_channels.append(launcher)
    agents = manager(launcher)

    await agents.reconcile((session,))
    launcher.processes[0].terminate()
    await launcher.processes[0].wait()
    await agents.reconcile((session,))

    assert len(launcher.processes) == 2
    assert agents.pool.clients[session.ident].uid == session.uid

    await agents.close()


@pytest.mark.asyncio
async def test_request_timeout_stops_unresponsive_agent(
    session_payload: list[dict[str, Any]],
    atspi_payload: dict[str, Any],
    manager_channels: list[FakeLauncher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = login_session(session_payload[0])
    launcher = FakeLauncher(atspi_window(atspi_payload), blocked=True)
    manager_channels.append(launcher)
    agents = manager(launcher)
    deadlines = []

    async def wait_for_timeout(operation: Awaitable[object], timeout: float) -> object:
        deadlines.append(timeout)

        if timeout == agents.request_timeout:
            task = asyncio.create_task(operation)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("agent request timed out")

        if not launcher.processes[0].task.done():
            operation.close()
            raise TimeoutError("agent shutdown timed out")

        return await operation

    monkeypatch.setattr(agent_module, "wait_for_timeout", wait_for_timeout)
    await agents.reconcile((session,))

    with pytest.raises(WindowAgentError, match="windows.*session 2"):
        await agents.windows(session)

    assert agents.pool.clients == {}
    assert launcher.processes[0].returncode is not None
    assert deadlines == [5.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_reconcile_does_not_suppress_unexpected_launcher_failure(
    session_payload: list[dict[str, Any]],
    atspi_payload: dict[str, Any],
    manager_channels: list[FakeLauncher],
) -> None:
    session = login_session(session_payload[0])
    launcher = FakeLauncher(atspi_window(atspi_payload))

    async def fail(session: LoginSession, fd: int) -> FakeProcess:
        raise RuntimeError("implementation bug")

    manager_channels.append(launcher)
    agents = manager(launcher)
    agents.launcher = fail

    with pytest.raises(RuntimeError, match="implementation bug"):
        await agents.reconcile((session,))


@pytest.mark.parametrize("phase", ["launch", "open"])
@pytest.mark.asyncio
async def test_cancelled_start_closes_sockets_and_releases_process(
    monkeypatch: pytest.MonkeyPatch, session_payload: list[dict[str, Any]], phase: str,
) -> None:

    sockets = (FakeSocket(7), FakeSocket(8))
    process = ResourceProcess()
    launch = (
        AsyncMock(side_effect=asyncio.CancelledError("launch cancelled"))
        if phase == "launch" else AsyncMock(return_value=process)
    )
    monkeypatch.setattr(agent_module, "socket", SimpleNamespace(socketpair=lambda *args: sockets, AF_UNIX=1, SOCK_STREAM=1))
    monkeypatch.setattr(agent_module.asyncio, "open_connection", AsyncMock(side_effect=asyncio.CancelledError("open cancelled")))
    agents = WindowAgentManager(Path("/test/sanex-window-agent"), launcher=launch)

    with pytest.raises(asyncio.CancelledError, match="cancelled"):
        await agents.reconcile((login_session(session_payload[0]),))

    assert all(channel.closed for channel in sockets)
    assert agents.pool.clients == {}
    assert process.wait_count == (1 if phase == "open" else 0)
    await agents.close()


@pytest.mark.asyncio
async def test_failed_final_assembly_removes_partial_pool_client(
    monkeypatch: pytest.MonkeyPatch, session_payload: list[dict[str, Any]],
) -> None:

    sockets = (FakeSocket(7), FakeSocket(8))
    writer = FakeWriter(sockets[0])
    process = ResourceProcess()
    process.stderr = asyncio.StreamReader()
    monkeypatch.setattr(agent_module, "socket", SimpleNamespace(socketpair=lambda *args: sockets, AF_UNIX=1, SOCK_STREAM=1))
    monkeypatch.setattr(agent_module.asyncio, "open_connection", AsyncMock(return_value=(asyncio.StreamReader(), writer)))

    def fail_task(coroutine: object) -> None:
        coroutine.close()
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(agent_module.asyncio, "create_task", fail_task)
    agents = WindowAgentManager(Path("/test/sanex-window-agent"), launcher=AsyncMock(return_value=process))

    with pytest.raises(RuntimeError, match="task creation failed"):
        await agents.reconcile((login_session(session_payload[0]),))

    assert agents.pool.clients == {}
    assert writer.closed and writer.waited
    assert process.wait_count == 1
    await agents.close()


@pytest.mark.asyncio
async def test_close_releases_all_agents_after_one_client_failure(
    monkeypatch: pytest.MonkeyPatch, session_payload: list[dict[str, Any]],
) -> None:

    processes = [ResourceProcess(), ResourceProcess()]
    writers = [FakeWriter(), FakeWriter()]
    processes[0].stderr = asyncio.StreamReader()
    monkeypatch.setattr(
        agent_module, "socket",
        SimpleNamespace(socketpair=lambda *args: (FakeSocket(), FakeSocket()), AF_UNIX=1, SOCK_STREAM=1),
    )
    monkeypatch.setattr(
        agent_module.asyncio, "open_connection",
        AsyncMock(side_effect=[(asyncio.StreamReader(), writer) for writer in writers]),
    )
    agents = WindowAgentManager(Path("/test/sanex-window-agent"), launcher=AsyncMock(side_effect=processes))
    await agents.reconcile(tuple(login_session(sample) for sample in session_payload[:2]))
    writers[0].wait_failure = RuntimeError("client cleanup failed")

    with pytest.raises(RuntimeError, match="client cleanup failed"):
        await agents.close()

    assert agents.pool.clients == {}
    assert all(process.wait_count == 1 for process in processes)
    assert all(writer.closed and writer.waited for writer in writers)
    await agents.close()
