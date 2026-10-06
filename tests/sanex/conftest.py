"""Shared fixtures for sanex tests."""

import json
import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sanelib.protocol import Event, parse_event

from tests.sanex.resource_fakes import MemoryWriter

from sanex.indicator import server as server_module


@pytest.fixture
def config_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load one complete valid configuration payload."""
    path = datafix_dir.parent.parent / "datafixtures" / "config.json"
    return json.loads(path.read_text())


@pytest.fixture
def runtime_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load one complete valid runtime-state payload."""
    path = datafix_dir.parent.parent / "datafixtures" / "runtime.json"
    return json.loads(path.read_text())


@pytest.fixture
def event_records(datafix_dir: Path) -> tuple[bytes, ...]:
    """Load valid event records exactly as stored in JSONL."""
    path = datafix_dir.parent.parent / "datafixtures" / "events.jsonl"
    return tuple(path.read_bytes().splitlines())


@pytest.fixture
def event_samples(event_records: tuple[bytes, ...]) -> tuple[Event, ...]:
    """Return valid sample events ordered by their sequence number."""
    return tuple(sorted((parse_event(record) for record in event_records), key=lambda event: event.seq))


@pytest.fixture
def account_discovery_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load AccountsService and NSS discovery samples."""
    path = datafix_dir.parent.parent / "datafixtures" / "accounts.json"
    return json.loads(path.read_text())


@pytest.fixture
def session_payload(datafix_dir: Path) -> list[dict[str, Any]]:
    """Load representative systemd-logind session records."""
    path = datafix_dir.parent.parent / "datafixtures" / "sessions.json"
    return json.loads(path.read_text())


@pytest.fixture
def atspi_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load representative AT-SPI object references and windows."""
    path = datafix_dir.parent.parent / "datafixtures" / "atspi.json"
    return json.loads(path.read_text())


@pytest.fixture
def process_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load representative procfs process identity fields."""
    path = datafix_dir.parent.parent / "datafixtures" / "process.json"
    return json.loads(path.read_text())


@pytest.fixture
def sync_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load representative synchronization protocol messages."""
    path = datafix_dir.parent.parent / "datafixtures" / "sync.json"
    return json.loads(path.read_text())


@pytest.fixture
def install_payload(datafix_dir: Path) -> dict[str, Any]:
    """Load representative system update tool paths."""
    path = datafix_dir.parent.parent / "datafixtures" / "install.json"
    return json.loads(path.read_text())


@pytest.fixture
def stream_pair() -> Callable[[], tuple[asyncio.StreamReader, MemoryWriter, asyncio.StreamReader, MemoryWriter]]:
    """Create connected in-memory stream boundaries for the real channel protocol."""
    def create() -> tuple[asyncio.StreamReader, MemoryWriter, asyncio.StreamReader, MemoryWriter]:
        root_reader = asyncio.StreamReader()
        agent_reader = asyncio.StreamReader()
        return root_reader, MemoryWriter(agent_reader), agent_reader, MemoryWriter(root_reader)

    return create


@pytest.fixture
def protected_ancestors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Represent trusted OS metadata without depending on host UID remapping."""
    original_stat = Path.stat
    ancestors = set(tmp_path.parents)

    def metadata(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        result = original_stat(path, *args, **kwargs)

        if path in ancestors:
            values = list(result)
            values[4] = 0
            values[0] &= ~0o022
            return os.stat_result(values)

        return result

    monkeypatch.setattr(Path, "stat", metadata)


@pytest.fixture
def unix_listener(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Expose real server handlers through a fake listener, not a Unix socket."""

    listener = SimpleNamespace(handler=None, closed=False, waited=False, permissions=[])

    def close() -> None:
        listener.closed = True

    async def wait_closed() -> None:
        listener.waited = True

    async def start(handler: Callable[..., Awaitable[None]], **kwargs: object) -> SimpleNamespace:
        listener.handler = handler
        return SimpleNamespace(close=close, wait_closed=wait_closed)

    monkeypatch.setattr(server_module.asyncio, "start_unix_server", start)
    monkeypatch.setattr(server_module, "os", SimpleNamespace(chmod=lambda path, mode: listener.permissions.append((path, mode))))
    return listener
