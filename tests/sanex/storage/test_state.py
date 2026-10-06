"""Tests for persistent per-account runtime state."""

import stat
from pathlib import Path
from typing import Any

import pytest

from sanex.exceptions import RuntimeStateError, StorageError
from sanex.model.state import decode_runtime_state
from sanex.storage.state import RuntimeStateStore


def test_store_keeps_uid_states_separate_and_private(
    tmp_path: Path,
    runtime_payload: dict[str, Any],
) -> None:
    store = RuntimeStateStore(tmp_path / "accounts")
    first = decode_runtime_state(runtime_payload)
    second = first.model_copy(deep=True)
    second.event_seq += 1

    assert store.load(1001) is None
    saved_first = store.save(1001, first)
    saved_second = store.save(1002, second)

    assert store.load(1001) == saved_first
    assert store.load(1002) == saved_second

    for uid in (1001, 1002):
        path = store.path_for(uid)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_invalid_mutated_state_does_not_replace_current_file(
    tmp_path: Path,
    runtime_payload: dict[str, Any],
) -> None:
    store = RuntimeStateStore(tmp_path / "accounts")
    state = decode_runtime_state(runtime_payload)
    store.save(1001, state)
    path = store.path_for(1001)
    original = path.read_bytes()
    state.prc_runs[0].reported = False

    with pytest.raises(RuntimeStateError, match="reported must be true"):
        store.save(1001, state)

    assert path.read_bytes() == original


def test_invalid_stored_state_is_rejected(
    tmp_path: Path,
    runtime_payload: dict[str, Any],
) -> None:
    store = RuntimeStateStore(tmp_path / "accounts")
    state = decode_runtime_state(runtime_payload)
    store.save(1001, state)
    store.path_for(1001).write_text("{}")

    with pytest.raises(RuntimeStateError, match="Field required"):
        store.load(1001)


@pytest.mark.parametrize("operation", ["load", "save"])
def test_filesystem_failures_use_application_exception(
    tmp_path: Path,
    runtime_payload: dict[str, Any],
    operation: str,
) -> None:
    accounts_root = tmp_path / "accounts"
    accounts_root.write_bytes(b"not a directory")
    store = RuntimeStateStore(accounts_root)

    with pytest.raises(StorageError):

        if operation == "load":
            store.load(1001)

        else:
            store.save(1001, decode_runtime_state(runtime_payload))
