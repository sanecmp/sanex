"""Length-prefixed Unix-socket transport for per-session window agents."""

import asyncio
import struct
from dataclasses import dataclass, field
from typing import Protocol

from ..exceptions import SaneaException, WindowAgentError
from ..model.session import LoginSession
from ..model.window import AtspiWindow, AtspiWindowSnapshot
from ..model.window_agent import (
    CloseRequest,
    CloseResponse,
    ErrorResponse,
    Request,
    Response,
    WindowsRequest,
    WindowsResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)


MAX_MESSAGE_SIZE = 1_048_576
_HEADER = struct.Struct("!I")


class WindowBackend(Protocol):
    """AT-SPI operations executed inside a user-session agent."""

    async def windows(self) -> AtspiWindowSnapshot: ...

    async def close_window(self, window: AtspiWindow) -> bool: ...


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one bounded length-prefixed message."""
    try:
        header = await reader.readexactly(_HEADER.size)
        size = _HEADER.unpack(header)[0]

        if size == 0 or size > MAX_MESSAGE_SIZE:
            raise WindowAgentError("read frame", f"invalid message size {size}")

        return await reader.readexactly(size)

    except asyncio.IncompleteReadError as error:
        raise WindowAgentError("read frame", "unexpected end of stream") from error


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write one bounded length-prefixed message."""

    if not data or len(data) > MAX_MESSAGE_SIZE:
        raise WindowAgentError("write frame", f"invalid message size {len(data)}")

    writer.write(_HEADER.pack(len(data)))
    writer.write(data)
    try:
        await writer.drain()

    except (ConnectionError, OSError) as error:
        raise WindowAgentError("write frame", error) from error


@dataclass(slots=True)
class WindowAgentClient:
    """Root-side connection bound to one trusted child agent and session."""

    uid: int
    sess_ident: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    _request_seq: int = field(default=0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:

        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise WindowAgentError("create client", "UID is invalid")

        if not self.sess_ident:
            raise WindowAgentError("create client", "session ident is empty")

    async def windows(self, session: LoginSession) -> AtspiWindowSnapshot:
        """Request a complete window snapshot for the bound session."""
        self._check_session(session.uid, session.ident)
        response = await self._exchange("windows")

        if not isinstance(response, WindowsResponse):
            raise WindowAgentError("windows", "agent returned an unexpected response")

        return response.snapshot

    async def close_window(
        self,
        uid: int,
        sess_ident: str,
        window: AtspiWindow,
    ) -> bool:
        """Request closing one concrete window in the bound session."""
        self._check_session(uid, sess_ident)
        response = await self._exchange("close", window)

        if not isinstance(response, CloseResponse):
            raise WindowAgentError("close", "agent returned an unexpected response")

        return response.accepted

    async def close(self) -> None:
        """Close the private agent channel."""
        self.writer.close()
        try:
            await self.writer.wait_closed()

        except (ConnectionError, OSError):
            pass

    async def _exchange(
        self,
        operation: str,
        window: AtspiWindow | None = None,
    ) -> Response:

        async with self._lock:
            self._request_seq += 1

            if operation == "windows":
                request: Request = WindowsRequest(
                    ident=self._request_seq,
                    op="windows",
                    sess_ident=self.sess_ident,
                )

            elif operation == "close" and window is not None:
                request = CloseRequest(
                    ident=self._request_seq,
                    op="close",
                    sess_ident=self.sess_ident,
                    window=window,
                )

            else:
                raise WindowAgentError(operation, "invalid client operation")

            await write_frame(self.writer, encode_request(request))
            response = decode_response(await read_frame(self.reader))

            if response.ident != request.ident:
                raise WindowAgentError(
                    operation,
                    f"response ident {response.ident} does not match {request.ident}",
                )

            if isinstance(response, ErrorResponse):
                raise WindowAgentError(operation, response.detail)

            return response

    def _check_session(self, uid: int, sess_ident: str) -> None:

        if uid != self.uid or sess_ident != self.sess_ident:
            raise WindowAgentError(
                "route",
                f"agent is bound to UID {self.uid} session {self.sess_ident}",
            )


@dataclass(slots=True)
class WindowAgentPool:
    """Route supervisor snapshots and close requests to session-bound agents."""

    clients: dict[str, WindowAgentClient] = field(default_factory=dict)

    def add(self, client: WindowAgentClient) -> None:
        """Register one connected agent without replacing an active channel."""

        if client.sess_ident in self.clients:
            raise WindowAgentError(
                "register",
                f"session {client.sess_ident} already has an agent",
            )

        self.clients[client.sess_ident] = client

    def remove(self, sess_ident: str) -> WindowAgentClient | None:
        """Remove and return a session agent."""
        return self.clients.pop(sess_ident, None)

    async def windows(self, session: LoginSession) -> AtspiWindowSnapshot:
        """Return a window snapshot from the exact UID/session agent."""
        return await self._client(session.ident, session.uid).windows(session)

    async def close_window(
        self,
        uid: int,
        sess_ident: str,
        window: AtspiWindow,
    ) -> bool:
        """Route a close request to the exact UID/session agent."""
        client = self._client(sess_ident, uid)
        return await client.close_window(uid, sess_ident, window)

    def _client(self, sess_ident: str, uid: int) -> WindowAgentClient:
        client = self.clients.get(sess_ident)

        if client is None:
            raise WindowAgentError("route", f"session {sess_ident} has no agent")

        if client.uid != uid:
            raise WindowAgentError(
                "route",
                f"session {sess_ident} agent UID {client.uid} does not match {uid}",
            )

        return client


@dataclass(slots=True)
class WindowAgentServer:
    """Agent-side request loop over one inherited private socket."""

    sess_ident: str
    backend: WindowBackend
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def serve(self) -> None:
        """Serve requests until the root side closes the private channel."""

        while True:
            try:
                data = await read_frame(self.reader)

            except WindowAgentError as error:

                if self.reader.at_eof():
                    return

                raise error

            request = decode_request(data)
            response = await self._handle(request)
            await write_frame(self.writer, encode_response(response))

    async def _handle(self, request: Request) -> Response:

        if request.sess_ident != self.sess_ident:
            return ErrorResponse(
                ident=request.ident,
                op="error",
                detail=f"agent is bound to session {self.sess_ident}",
            )

        try:

            if isinstance(request, WindowsRequest):
                return WindowsResponse(
                    ident=request.ident,
                    op="windows",
                    snapshot=await self.backend.windows(),
                )

            return CloseResponse(
                ident=request.ident,
                op="close",
                accepted=await self.backend.close_window(request.window),
            )

        except SaneaException as error:
            return ErrorResponse(
                ident=request.ident,
                op="error",
                detail=f"{error}" or type(error).__name__,
            )
