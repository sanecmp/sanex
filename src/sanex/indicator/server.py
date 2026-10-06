"""Read-only Unix-socket service for desktop indicators."""

import asyncio
from asyncio import wait_for as wait_for_timeout
import os
import socket
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..exceptions import IndicatorError, IndicatorProtocolError
from ..model.indicator import (
    IndicatorRequest,
    IndicatorStatus,
    parse_indicator_request,
)


DEFAULT_INDICATOR_SOCKET_PATH = Path("/run/sanex/indicator.sock")
MAX_REQUEST_SIZE = 1024
MAX_CONNECTIONS_PER_SESSION = 4
REQUEST_TIMEOUT = 5
_PEER_CREDENTIALS = struct.Struct("3i")

StatusProvider = Callable[[int, str, int], IndicatorStatus | None]
SessionAuthorizer = Callable[[int, str], bool]
SubscriberKey = tuple[int, str]
StatusKey = tuple[int | None, int]


@dataclass(slots=True)
class IndicatorStatusServer:
    """Publish changed session status to authenticated local users."""

    path: Path
    authorize: SessionAuthorizer
    provide_status: StatusProvider
    mode: int = 0o666
    _server: asyncio.Server | None = field(default=None, init=False, repr=False)
    _subscribers: dict[SubscriberKey, set[asyncio.Queue[bytes | None]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _snapshots: dict[SubscriberKey, bytes] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _status_keys: dict[SubscriberKey, StatusKey] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _writers: set[asyncio.StreamWriter] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _tasks: set[asyncio.Task[None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )

    async def start(self) -> None:
        """Create the socket and begin accepting indicator connections."""

        if self._server is not None:
            return

        path = self.path
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        self._remove_stale_socket()
        try:
            self._server = await asyncio.start_unix_server(
                self._handle,
                path=path,
                limit=MAX_REQUEST_SIZE + 1,
            )
            os.chmod(path, self.mode)

        except OSError as error:
            await self.close()
            raise IndicatorError("start", error) from error

    def publish(self, timestamp: int) -> None:
        """Queue only changed status snapshots for connected sessions."""
        snapshots = self._snapshots
        status_keys = self._status_keys

        for key, queues in tuple(self._subscribers.items()):
            uid, sess_ident = key
            status = self.provide_status(uid, sess_ident, timestamp)

            if status is None:

                for queue in tuple(queues):
                    self._replace_queued(queue, None)

                snapshots.pop(key, None)
                status_keys.pop(key, None)
                continue

            status_key = self._build_status_key(status)

            if status_key == status_keys.get(key):
                continue

            snapshot = status.encode()
            status_keys[key] = status_key
            snapshots[key] = snapshot

            for queue in tuple(queues):
                self._replace_queued(queue, snapshot)

    async def close(self) -> None:
        """Stop accepting clients, close subscribers and remove the socket."""
        server = self._server
        subscribers = self._subscribers
        snapshots = self._snapshots
        status_keys = self._status_keys
        writers = self._writers
        tasks = self._tasks
        self._server = None

        if server is not None:
            server.close()

        for queues in tuple(subscribers.values()):

            for queue in tuple(queues):
                self._replace_queued(queue, None)

        active_writers = tuple(writers)

        for writer in active_writers:
            writer.close()

        active_tasks = tuple(tasks)

        for task in active_tasks:
            task.cancel()

        if active_tasks:
            await asyncio.gather(
                *active_tasks,
                return_exceptions=True,
            )

        if server is not None:
            await server.wait_closed()

        subscribers.clear()
        snapshots.clear()
        status_keys.clear()
        writers.clear()
        tasks.clear()
        self._remove_owned_socket()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        subscribers_by_key = self._subscribers
        snapshots = self._snapshots
        status_keys = self._status_keys
        writers = self._writers
        tasks = self._tasks
        task = asyncio.current_task()

        if task is not None:
            tasks.add(task)

        writers.add(writer)
        key: SubscriberKey | None = None
        queue: asyncio.Queue[bytes | None] | None = None
        try:
            uid = self._get_peer_uid(writer)
            request = await self._read_request(reader)
            key = (uid, request.sess_ident)

            if not self.authorize(*key):
                return

            subscribers = subscribers_by_key.setdefault(key, set())

            if len(subscribers) >= MAX_CONNECTIONS_PER_SESSION:
                return

            queue = asyncio.Queue(maxsize=1)
            subscribers.add(queue)
            snapshot = snapshots.get(key)

            if snapshot is not None:
                queue.put_nowait(snapshot)

            disconnect_task = asyncio.create_task(reader.read())
            status_task = None
            try:

                while True:
                    status_task = asyncio.create_task(queue.get())
                    done, pending_tasks = await asyncio.wait(
                        (status_task, disconnect_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if disconnect_task in done:
                        status_task.cancel()
                        await asyncio.gather(status_task, return_exceptions=True)
                        return

                    snapshot = status_task.result()

                    if snapshot is None:
                        return

                    writer.write(snapshot)
                    await writer.drain()

            finally:
                pending = (disconnect_task,) if status_task is None else (disconnect_task, status_task)

                for pending_task in pending:

                    if not pending_task.done():
                        pending_task.cancel()

                await asyncio.gather(*pending, return_exceptions=True)

        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            ConnectionError,
            IndicatorProtocolError,
            OSError,
            ValueError,
        ):
            return

        finally:

            if key is not None and queue is not None:
                subscribers = subscribers_by_key.get(key)

                if subscribers is not None:
                    subscribers.discard(queue)

                    if not subscribers:
                        subscribers_by_key.pop(key, None)
                        snapshots.pop(key, None)
                        status_keys.pop(key, None)

            writers.discard(writer)

            if task is not None:
                tasks.discard(task)

            writer.close()

    async def _read_request(self, reader: asyncio.StreamReader) -> IndicatorRequest:
        data = await wait_for_timeout(reader.readline(), timeout=REQUEST_TIMEOUT)

        if not data or len(data) > MAX_REQUEST_SIZE or not data.endswith(b"\n"):
            raise ValueError("invalid indicator request frame")

        return parse_indicator_request(data)

    @staticmethod
    def _get_peer_uid(writer: asyncio.StreamWriter) -> int:
        connection = writer.get_extra_info("socket")

        if connection is None:
            raise ValueError("indicator connection has no socket")

        credentials = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _PEER_CREDENTIALS.size,
        )
        process_ident, uid, group_ident = _PEER_CREDENTIALS.unpack(credentials)
        return uid

    @staticmethod
    def _replace_queued(queue: asyncio.Queue[bytes | None], value: bytes | None) -> None:

        if queue.full():
            queue.get_nowait()

        queue.put_nowait(value)

    @staticmethod
    def _build_status_key(status: IndicatorStatus) -> StatusKey:
        remaining = status.remaining
        remaining_minutes = None if remaining is None else (remaining + 59) // 60
        break_minutes = (status.break_duration + 59) // 60
        return remaining_minutes, break_minutes

    def _remove_stale_socket(self) -> None:
        path = self.path
        try:
            mode = path.lstat().st_mode

        except FileNotFoundError:
            return

        if not stat.S_ISSOCK(mode):
            raise IndicatorError("start", f"path is not a socket: {path}")

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(f"{path}")

        except ConnectionRefusedError:
            pass

        except OSError as error:
            raise IndicatorError("probe existing socket", error) from error

        else:
            raise IndicatorError("start", f"socket is already accepting: {path}")

        finally:
            probe.close()
        try:
            path.unlink()

        except OSError as error:
            raise IndicatorError("remove stale socket", error) from error

    def _remove_owned_socket(self) -> None:
        path = self.path
        try:
            mode = path.lstat().st_mode

        except FileNotFoundError:
            return

        if not stat.S_ISSOCK(mode):
            return

        try:
            path.unlink()

        except OSError:
            pass
