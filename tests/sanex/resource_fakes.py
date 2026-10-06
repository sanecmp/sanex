"""External socket, stream and process boundaries without real connections."""

import asyncio
import struct


class FakeSocket:
    def __init__(self, fd: int = 7, uid: int = 1001) -> None:
        self.fd = fd
        self.uid = uid
        self.closed = False
        self.close_count = 0
        self.blocking = True
        self.options: list[tuple[int, int, int]] = []
        self.address: tuple[str, int] | None = None

    def fileno(self) -> int:
        return self.fd

    def setblocking(self, blocking: bool) -> None:
        self.blocking = blocking

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        return struct.pack("3i", 1234, self.uid, 1001)

    def bind(self, address: tuple[str, int]) -> None:
        self.address = address

    def close(self) -> None:
        self.close_count += 1
        self.closed = True


class FakeWriter:
    def __init__(self, peer: FakeSocket | None = None) -> None:
        self.peer = peer or FakeSocket()
        self.writes: list[bytes] = []
        self.closed = False
        self.waited = False
        self.drained = asyncio.Event()
        self.failure: Exception | None = None
        self.wait_failure: Exception | None = None

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.drained.set()

        if self.failure is not None:
            raise self.failure

    def get_extra_info(self, name: str) -> FakeSocket | None:
        return self.peer if name == "socket" else None

    def close(self) -> None:
        self.closed = True
        self.peer.close()

    async def wait_closed(self) -> None:
        self.waited = True

        if self.wait_failure is not None:
            raise self.wait_failure


class FakeProcess:
    def __init__(self, *, blocked: bool = False) -> None:
        self.returncode: int | None = None
        self.stderr: asyncio.StreamReader | None = None
        self.finished = asyncio.Event()
        self.wait_count = 0
        self.terminated = False
        self.killed = False

        if not blocked:
            self.finished.set()

    async def wait(self) -> int:
        self.wait_count += 1
        await self.finished.wait()
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.finished.set()

    def kill(self) -> None:
        self.killed = True
        self.finished.set()


class MemoryWriter(FakeWriter):
    def __init__(self, receiver: asyncio.StreamReader) -> None:
        super().__init__()
        self.receiver = receiver

    def write(self, data: bytes) -> None:
        super().write(data)
        self.receiver.feed_data(data)

    def close(self) -> None:
        super().close()
        self.receiver.feed_eof()
