"""Tests for authenticated synchronization HTTP channels."""

import hashlib
import json
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest
from sanelib.protocol import SyncRequest, parse_sync_request

from sanex.exceptions import (
    EventPacketError,
    SyncHttpStatusError,
    SyncProtocolError,
    SyncTransportError,
    TlsConfigError,
)
from sanex.storage.events import EventPacket
from sanex.storage.pki import PkiPaths
from sanex.sync.client import EventUploadResult, LogUploadResult, SyncHttpClient


BASE_URL = "https://192.0.2.10:8443"


def tls_context() -> ssl.SSLContext:
    """Build a context for MockTransport, which performs no TLS handshake."""
    context = ssl.create_default_context()
    context.check_hostname = False
    return context


def control_request(sync_payload: dict[str, Any]) -> SyncRequest:
    """Build the representative strict control request."""
    return parse_sync_request(json.dumps(sync_payload["request"]))


@pytest.mark.asyncio
async def test_control_exchange_uses_exact_endpoint_and_validates_response(
    sync_payload: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == f"{BASE_URL}/client/sync"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == sync_payload["request"]
        return httpx.Response(200, json=sync_payload["response"])

    client = SyncHttpClient(tls_context(), httpx.MockTransport(handler))

    response = await client.sync(BASE_URL, control_request(sync_payload))
    await client.close()

    assert response.commands[0].ident == 92


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (204, LogUploadResult.SAVED),
        (413, LogUploadResult.TOO_LARGE),
        (422, LogUploadResult.INVALID_ENCODING),
    ],
)
async def test_log_upload_maps_documented_terminal_statuses(
    status: int,
    expected: LogUploadResult,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url == f"{BASE_URL}/client/log"
        assert request.content == "ошибка\n".encode()
        return httpx.Response(status)

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(handler),
    ) as client:
        result = await client.upload_log(BASE_URL, "ошибка\n".encode())

    assert result is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (201, EventUploadResult.SAVED),
        (204, EventUploadResult.ALREADY_SAVED),
        (403, EventUploadResult.DISABLED),
        (404, EventUploadResult.ACCOUNT_MISSING),
        (409, EventUploadResult.SEQUENCE_CONFLICT),
        (422, EventUploadResult.INVALID_PACKET),
    ],
)
async def test_event_upload_sends_exact_packet_bytes(
    tmp_path: Path,
    event_records: tuple[bytes, ...],
    status: int,
    expected: EventUploadResult,
) -> None:
    content = b"\n".join(event_records) + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / "packet.jsonl"
    path.write_bytes(content)
    packet = EventPacket(1001, path, 120, 141, digest)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == f"{BASE_URL}/client/events/1001/{digest}"
        assert request.headers["content-type"] == "application/x-ndjson"
        assert request.content == content
        return httpx.Response(status)

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(handler),
    ) as client:
        result = await client.upload_events(BASE_URL, packet)

    assert result is expected


@pytest.mark.asyncio
async def test_changed_event_packet_is_rejected_before_network(tmp_path: Path) -> None:
    path = tmp_path / "packet.jsonl"
    path.write_bytes(b"changed\n")
    packet = EventPacket(1001, path, 1, 1, "0" * 64)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(201)

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(handler),
    ) as client:

        with pytest.raises(EventPacketError, match="changed before upload"):
            await client.upload_events(BASE_URL, packet)

    assert calls == 0


@pytest.mark.asyncio
async def test_network_and_transient_statuses_are_not_terminal(
    sync_payload: dict[str, Any],
) -> None:
    request = control_request(sync_payload)

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(unavailable),
    ) as client:

        with pytest.raises(SyncHttpStatusError, match="HTTP 503"):
            await client.sync(BASE_URL, request)

    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(disconnected),
    ) as client:

        with pytest.raises(SyncTransportError, match="transport failed"):
            await client.sync(BASE_URL, request)


@pytest.mark.asyncio
async def test_untrusted_base_url_is_rejected_before_network(
    sync_payload: dict[str, Any],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(handler),
    ) as client:

        with pytest.raises(SyncProtocolError, match="absolute HTTPS origin"):
            await client.sync(
                "http://user:secret@example.test/api?x=1",
                control_request(sync_payload),
            )

    assert calls == 0


def test_missing_pki_material_is_reported_in_application_hierarchy(
    tmp_path: Path,
) -> None:

    with pytest.raises(TlsConfigError, match="unable to load sanex PKI"):
        PkiPaths(tmp_path / "missing").client_context()


@pytest.mark.asyncio
async def test_server_cookies_are_not_retained_between_requests(
    sync_payload: dict[str, Any],
) -> None:
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            json=sync_payload["response"],
            headers={"Set-Cookie": f"attempt={len(cookies)}; Path=/"},
        )

    async with SyncHttpClient(
        tls_context(),
        httpx.MockTransport(handler),
    ) as client:
        request = control_request(sync_payload)
        await client.sync(BASE_URL, request)
        await client.sync(BASE_URL, request)

    assert cookies == [None, None]
