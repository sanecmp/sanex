"""Lifecycle management for privileged per-session window-agent processes."""

import asyncio
from asyncio import wait_for as wait_for_timeout
import logging
import math
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from ..exceptions import SaneaException, WindowAgentError
from ..model.session import LoginSession
from ..model.window import AtspiWindow, AtspiWindowSnapshot
from ..platform.window_agent import WindowAgentClient, WindowAgentPool
from ..utils.resources import close_async_resources


DEFAULT_REQUEST_TIMEOUT = 5.0
DEFAULT_SHUTDOWN_TIMEOUT = 2.0
_SAFE_ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")


class AgentProcess(Protocol):
    """Subset of asyncio subprocess operations required by the manager."""

    @property
    def returncode(self) -> int | None: ...

    @property
    def stderr(self) -> asyncio.StreamReader | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


AgentLauncher = Callable[[LoginSession, int], Awaitable[AgentProcess]]


@dataclass(slots=True)
class _ManagedAgent:
    session: LoginSession
    process: AgentProcess
    client: WindowAgentClient
    stderr_task: asyncio.Task[None] | None


@dataclass(slots=True)
class WindowAgentManager:
    """Start, route, replace and stop one child agent per logind session."""

    executable: Path
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT
    launcher: AgentLauncher | None = None
    pool: WindowAgentPool = field(default_factory=WindowAgentPool)
    _agents: dict[str, _ManagedAgent] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:

        if not isinstance(self.executable, Path):
            raise WindowAgentError("configure", "agent executable must be a path")

        if not self.executable.is_absolute():
            raise WindowAgentError("configure", "agent executable path must be absolute")

        for name, value in (
            ("request", self.request_timeout),
            ("shutdown", self.shutdown_timeout),
        ):

            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise WindowAgentError(
                    "configure",
                    f"agent {name} timeout must be a positive finite number",
                )

    async def reconcile(self, sessions: tuple[LoginSession, ...]) -> None:
        """Match running agents to the current relevant logind sessions."""
        desired = {session.ident: session for session in sessions}

        if len(desired) != len(sessions):
            raise WindowAgentError("reconcile", "duplicate session ident")

        for sess_ident, agent in tuple(self._agents.items()):
            session = desired.get(sess_ident)

            if (
                session is None
                or session.uid != agent.session.uid
                or agent.process.returncode is not None
            ):
                await self._stop(sess_ident)

        for session in sessions:

            if session.ident in self._agents:
                continue

            try:
                await self._start(session)

            except (SaneaException, OSError) as error:
                logger.warning(
                    "Unable to start window agent for session %s of UID %s: %s",
                    session.ident,
                    session.uid,
                    error,
                )

    async def windows(self, session: LoginSession) -> AtspiWindowSnapshot:
        """Request a bounded-time snapshot from the exact session agent."""
        return await self._request(
            session.ident,
            self.pool.windows(session),
            "windows",
        )

    async def close_window(
        self,
        uid: int,
        sess_ident: str,
        window: AtspiWindow,
    ) -> bool:
        """Request a bounded-time close from the exact UID/session agent."""
        return await self._request(
            sess_ident,
            self.pool.close_window(uid, sess_ident, window),
            "close",
        )

    async def close(self) -> None:
        """Stop all managed agents and release their private channels."""
        await close_async_resources(
            *(("stop window agent", lambda ident=ident: self._stop(ident)) for ident in tuple(self._agents)),
        )

    async def _start(self, session: LoginSession) -> None:
        parent_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        process = None
        writer = None
        client = None
        stderr_task = None
        transferred = False
        child_owned = True

        try:
            parent_socket.setblocking(False)
            process = await self._launch(session, child_socket.fileno())
            child_owned = False
            child_socket.close()
            reader, writer = await asyncio.open_connection(sock=parent_socket)
            client = WindowAgentClient(session.uid, session.ident, reader, writer)
            self.pool.add(client)
            stderr = process.stderr

            if stderr is not None:
                stderr_task = asyncio.create_task(self._drain_stderr(session, stderr))

            self._agents[session.ident] = _ManagedAgent(session, process, client, stderr_task)
            transferred = True

        finally:

            if not transferred:
                operations = []

                if child_owned:
                    operations.append(("close child socket", child_socket.close))

                if writer is None:
                    operations.append(("close parent socket", parent_socket.close))

                else:
                    operations.extend((
                        ("close parent stream", writer.close),
                        ("wait for parent stream", writer.wait_closed),
                    ))

                if client is not None and self.pool.clients.get(session.ident) is client:
                    self.pool.remove(session.ident)

                if process is not None:
                    operations.append(("release window-agent process", lambda: self._terminate_process(process)))

                if stderr_task is not None:
                    operations.append(("release window-agent stderr task", lambda: self._finish_stderr(stderr_task)))

                await close_async_resources(*operations)

    async def _launch(self, session: LoginSession, fd: int) -> AgentProcess:

        if self.launcher is not None:
            return await self.launcher(session, fd)

        return await asyncio.create_subprocess_exec(
            os.fspath(self.executable),
            "--fd",
            f"{fd}",
            "--uid",
            f"{session.uid}",
            "--session",
            session.ident,
            pass_fds=(fd,),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=_SAFE_ENVIRONMENT,
        )

    async def _request(
        self,
        sess_ident: str,
        operation: Awaitable[ResultT],
        name: str,
    ) -> ResultT:
        try:
            return await wait_for_timeout(operation, timeout=self.request_timeout)

        except asyncio.CancelledError:
            raise

        except (SaneaException, OSError) as error:
            await self._stop(sess_ident)
            raise WindowAgentError(
                name,
                f"session {sess_ident}: {error}",
            ) from error

    async def _stop(self, sess_ident: str) -> None:
        agent = self._agents.pop(sess_ident, None)
        client = self.pool.remove(sess_ident)
        operations = []

        if client is not None:
            operations.append(("close window-agent client", client.close))

        if agent is not None:
            operations.append(("release window-agent process", lambda: self._terminate_process(agent.process)))

            if agent.stderr_task is not None:
                operations.append(("release window-agent stderr task", lambda: self._finish_stderr(agent.stderr_task)))

        await close_async_resources(*operations)

    @staticmethod
    async def _finish_stderr(task: asyncio.Task[None]) -> None:

        if not task.done():
            task.cancel()

        result, = await asyncio.gather(task, return_exceptions=True)

        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result

    async def _terminate_process(self, process: AgentProcess) -> None:

        if process.returncode is not None:
            await process.wait()
            return

        try:
            await wait_for_timeout(process.wait(), timeout=self.shutdown_timeout)
            return

        except TimeoutError:
            process.terminate()

        try:
            await wait_for_timeout(process.wait(), timeout=self.shutdown_timeout)
            return

        except TimeoutError:
            process.kill()

        await process.wait()

    @staticmethod
    async def _drain_stderr(
        session: LoginSession,
        stream: asyncio.StreamReader,
    ) -> None:

        while line := await stream.readline():
            logger.warning(
                "Window agent session %s stderr: %s",
                session.ident,
                line.decode(errors="replace").rstrip(),
            )
