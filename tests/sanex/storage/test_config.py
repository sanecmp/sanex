"""Tests for persistent configuration snapshots."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from sanex.exceptions import ConfigError, StorageError
from sanex.storage.config import ConfigStore


def encode_payload(payload: object) -> bytes:
    """Encode a fixture payload without changing its data."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def test_store_roundtrip_preserves_validated_bytes(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    data = encode_payload(config_payload)

    assert store.load() is None
    saved = store.save(data)

    assert saved.ident == config_payload["ident"]
    assert store.load() == saved
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_snapshot_does_not_replace_current_file(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    original = encode_payload(config_payload)
    store.save(original)

    with pytest.raises(ConfigError):
        store.save(b"{}")

    assert path.read_bytes() == original


def test_read_failure_uses_application_exception(tmp_path: Path) -> None:

    with pytest.raises(StorageError, match="Failed to read"):
        ConfigStore(tmp_path).load()
