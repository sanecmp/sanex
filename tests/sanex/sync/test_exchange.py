"""Tests for one complete sanea synchronization exchange."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sanelib.protocol import parse_sync_request

from sanex.storage.events import EventPacket
from sanex.sync.client import EventUploadResult, LogUploadResult
from sanex.sync.discovery import SaneaEndpoint
from sanex.sync.exchange import SyncExchange, SyncSnapshot
from sanex.sync.protocol import parse_sync_response


def packet(tmp_path: Path, uid: int, first_seq: int) -> EventPacket:
    """Build a packet identity sufficient for the mocked HTTP adapter."""
    return EventPacket(
        uid=uid,
        path=tmp_path / f"{uid}-{first_seq}.jsonl",
        first_seq=first_seq,
        last_seq=first_seq,
        sha256=f"{first_seq:064x}",
    )


@pytest.mark.asyncio
async def test_exchange_runs_channels_in_order_and_acknowledges_successes(
    tmp_path: Path,
    sync_payload: dict[str, Any],
) -> None:
    request = parse_sync_request(json.dumps(sync_payload["request"]))
    response = parse_sync_response(json.dumps(sync_payload["response"]))
    packets = (
        packet(tmp_path, 1002, 1),
        packet(tmp_path, 1001, 3),
        packet(tmp_path, 1001, 2),
        packet(tmp_path, 1001, 4),
    )
    calls: list[str] = []

    class State:
        async def prepare(self) -> SyncSnapshot:
            calls.append("prepare")
            return SyncSnapshot(request, b"tail\n", packets)

        async def accept_control(
            self,
            received_snapshot: SyncSnapshot,
            received: object,
        ) -> None:
            assert received_snapshot.request == request
            assert received == response
            calls.append("control")

        async def acknowledge(self, event_packet: EventPacket) -> None:
            calls.append(f"ack:{event_packet.uid}:{event_packet.first_seq}")

        async def finish(self) -> None:
            calls.append("finish")

    results = iter(
        (
            EventUploadResult.SAVED,
            EventUploadResult.SEQUENCE_CONFLICT,
            EventUploadResult.ALREADY_SAVED,
            EventUploadResult.ALREADY_SAVED,
        )
    )

    async def upload_events(base_url: str, event_packet: EventPacket) -> EventUploadResult:
        calls.append(f"event:{event_packet.uid}:{event_packet.first_seq}")
        return next(results)

    client = SimpleNamespace(
        sync=AsyncMock(side_effect=lambda *arguments: calls.append("sync") or response),
        upload_log=AsyncMock(
            side_effect=lambda *arguments: calls.append("log") or LogUploadResult.SAVED,
        ),
        upload_events=upload_events,
    )
    discovery = SimpleNamespace(
        discover=AsyncMock(
            side_effect=lambda: calls.append("discover")
            or SaneaEndpoint("192.0.2.10", 8_443)
        ),
    )

    successful = await SyncExchange(State(), client, discovery).run()

    assert successful
    assert calls == [
        "prepare",
        "discover",
        "sync",
        "control",
        "log",
        "event:1001:2",
        "ack:1001:2",
        "event:1001:3",
        "event:1001:4",
        "ack:1001:4",
        "event:1002:1",
        "ack:1002:1",
        "finish",
    ]


@pytest.mark.asyncio
async def test_exchange_stops_before_http_when_sanea_is_not_found(
    sync_payload: dict[str, Any],
) -> None:
    request = parse_sync_request(json.dumps(sync_payload["request"]))
    state = SimpleNamespace(
        prepare=AsyncMock(return_value=SyncSnapshot(request, b"", ())),
    )
    client = SimpleNamespace(sync=AsyncMock())
    discovery = SimpleNamespace(discover=AsyncMock(return_value=None))

    successful = await SyncExchange(state, client, discovery).run()

    assert not successful
    state.prepare.assert_awaited_once_with()
    client.sync.assert_not_awaited()
