"""Bounded authenticated HTTP transport for sanex synchronization channels."""

import hashlib
import re
import ssl
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType

import httpx
from sanelib.protocol import SyncRequest, SyncResponse, encode_sync_request

from ..exceptions import (
    EventPacketError,
    SyncHttpStatusError,
    SyncProtocolError,
    SyncTransportError,
)
from ..storage.events import EventPacket
from .http import https_origin_url, request_timeout
from .protocol import parse_sync_response


MAX_CONNECTIONS = 2
MAX_KEEPALIVE_CONNECTIONS = 1
KEEPALIVE_EXPIRY = 30.0
MAX_CONTROL_RESPONSE_SIZE = 1_048_576


class LogUploadResult(StrEnum):
    """Documented terminal result of one technical-log attempt."""

    SAVED = "saved"
    TOO_LARGE = "too_large"
    INVALID_ENCODING = "invalid_encoding"


class EventUploadResult(StrEnum):
    """Documented terminal result of one immutable event-packet attempt."""

    SAVED = "saved"
    ALREADY_SAVED = "already_saved"
    DISABLED = "disabled"
    ACCOUNT_MISSING = "account_missing"
    SEQUENCE_CONFLICT = "sequence_conflict"
    INVALID_PACKET = "invalid_packet"


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    content: bytes


@dataclass(slots=True)
class SyncHttpClient:
    """Own one hardened long-lived httpx client for all sanea channels."""

    ssl_context: ssl.SSLContext
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        timeout = request_timeout()
        limits = httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=KEEPALIVE_EXPIRY,
        )
        self._client = httpx.AsyncClient(
            verify=self.ssl_context,
            timeout=timeout,
            limits=limits,
            trust_env=False,
            follow_redirects=False,
            http2=False,
            transport=self.transport,
        )

    async def __aenter__(self) -> "SyncHttpClient":
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close pooled HTTP connections."""
        await self._client.aclose()

    async def sync(self, base_url: str, request: SyncRequest) -> SyncResponse:
        """Perform and validate the authenticated control exchange."""
        response = await self._request(
            "sync",
            "POST",
            self._url(base_url, "/client/sync"),
            content=encode_sync_request(request),
            headers={"Content-Type": "application/json"},
            response_limit=MAX_CONTROL_RESPONSE_SIZE,
        )

        if response.status != 200:
            raise SyncHttpStatusError("sync", response.status)

        return parse_sync_response(response.content)

    async def upload_log(self, base_url: str, content: bytes) -> LogUploadResult:
        """Upload one already bounded UTF-8 technical-log tail."""
        response = await self._request(
            "log upload",
            "PUT",
            self._url(base_url, "/client/log"),
            content=content,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        results = {
            204: LogUploadResult.SAVED,
            413: LogUploadResult.TOO_LARGE,
            422: LogUploadResult.INVALID_ENCODING,
        }
        result = results.get(response.status)

        if result is None:
            raise SyncHttpStatusError("log upload", response.status)

        return result

    async def upload_events(
        self,
        base_url: str,
        packet: EventPacket,
    ) -> EventUploadResult:
        """Upload exact sealed packet bytes without reserialization."""

        if (
            not isinstance(packet.uid, int)
            or isinstance(packet.uid, bool)
            or packet.uid < 0
            or re.fullmatch(r"[0-9a-f]{64}", packet.sha256) is None
        ):
            raise EventPacketError("pending packet identity is invalid")

        try:
            content = packet.path.read_bytes()

        except OSError as error:
            raise EventPacketError(f"unable to read pending packet {packet.path}: {error}") from error

        if hashlib.sha256(content).hexdigest() != packet.sha256:
            raise EventPacketError(
                f"pending packet changed before upload: {packet.sha256}"
            )

        response = await self._request(
            "event upload",
            "POST",
            self._url(
                base_url,
                f"/client/events/{packet.uid}/{packet.sha256}",
            ),
            content=content,
            headers={"Content-Type": "application/x-ndjson"},
        )
        results = {
            201: EventUploadResult.SAVED,
            204: EventUploadResult.ALREADY_SAVED,
            403: EventUploadResult.DISABLED,
            404: EventUploadResult.ACCOUNT_MISSING,
            409: EventUploadResult.SEQUENCE_CONFLICT,
            422: EventUploadResult.INVALID_PACKET,
        }
        result = results.get(response.status)

        if result is None:
            raise SyncHttpStatusError("event upload", response.status)

        return result

    async def _request(
        self,
        operation: str,
        method: str,
        url: httpx.URL,
        content: bytes,
        headers: dict[str, str],
        response_limit: int = 0,
    ) -> _Response:
        try:

            async with self._client.stream(
                method,
                url,
                content=content,
                headers=headers,
            ) as response:
                received = bytearray()

                if response_limit:

                    async for chunk in response.aiter_bytes():
                        received.extend(chunk)

                        if len(received) > response_limit:
                            raise SyncProtocolError(
                                "$",
                                f"{operation} response exceeds {response_limit} bytes",
                            )

                return _Response(response.status_code, bytes(received))

        except SyncProtocolError:
            raise

        except httpx.HTTPError as error:
            raise SyncTransportError(operation, error) from error

        finally:
            self._client.cookies.clear()

    @staticmethod
    def _url(base_url: str, path: str) -> httpx.URL:
        try:
            return https_origin_url(base_url, path)

        except ValueError as error:
            raise SyncProtocolError("$.base_url", f"{error}") from error
