"""Tests for the service-owned synchronization state bridge."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from sanelib.protocol import DiscoveredAccount, Event, decode_config

from sanex.service.sync import ServiceSyncState
from sanex.storage.events import EventStore
from sanex.sync.commands import CommandStore
from sanex.sync.log import TechnicalLog
from sanex.sync.protocol import parse_sync_response


@pytest.mark.asyncio
async def test_prepares_and_accepts_complete_sync_state(
    tmp_path: Path,
    config_payload: dict[str, Any],
    sync_payload: dict[str, Any],
    event_samples: tuple[Event, ...],
) -> None:
    config = decode_config(config_payload)
    accounts_root = tmp_path / "accounts"
    initial_events = EventStore(accounts_root)
    initial_events.append(1001, event_samples[0])
    initial_events.append(1002, event_samples[0])
    events = EventStore(accounts_root)
    commands = CommandStore(tmp_path / "commands.json")
    log_path = tmp_path / "sanex.log"
    log_path.write_bytes(b"tail\n")
    accounts = (
        DiscoveredAccount(uid=1001, login="child", name="Child"),
    )
    apply_config = AsyncMock(return_value=True)
    command_executor = SimpleNamespace(run=AsyncMock())
    update_recovery = SimpleNamespace(reconcile=Mock())
    state = ServiceSyncState(
        config=lambda: config,
        apply_config=apply_config,
        state_lock=asyncio.Lock(),
        event_store=events,
        command_store=commands,
        command_executor=command_executor,
        update_recovery=update_recovery,
        accounts=SimpleNamespace(discover=AsyncMock(return_value=accounts)),
        technical_log=TechnicalLog(log_path),
        hostname=lambda: "child-laptop",
        package_version=lambda: "0.1.0",
    )

    snapshot = await state.prepare()

    assert snapshot.request.hostname == "child-laptop"
    assert snapshot.request.accounts == accounts
    assert snapshot.log_tail == b"tail\n"
    assert tuple(packet.uid for packet in snapshot.packets) == (1001, 1002)
    assert not events.open_path(1001).read_bytes()
    assert not events.open_path(1002).read_bytes()

    sync_payload["response"]["config"] = config_payload
    response = parse_sync_response(json.dumps(sync_payload["response"]))
    await state.accept_control(snapshot, response)
    assert commands.pending == response.commands
    update_recovery.reconcile.assert_called_once_with()

    await state.acknowledge(snapshot.packets[0])
    assert not events.pending(1001)

    await state.finish()
    command_executor.run.assert_awaited_once_with()
    apply_config.assert_awaited_once_with(response.config.model_dump_json())
