"""Tests for durable atomic file replacement."""

import stat
from pathlib import Path

import pytest

from sanex.exceptions import StorageError
from sanex.storage.atomic import atomic_write_bytes


def test_atomic_write_replaces_contents_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    atomic_write_bytes(path, b"first")
    atomic_write_bytes(path, b"second")

    assert path.read_bytes() == b"second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_failure_preserves_target_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"original")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("sanex.storage.atomic.os.replace", fail_replace)

    with pytest.raises(StorageError, match="replace failed"):
        atomic_write_bytes(path, b"replacement")

    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]
