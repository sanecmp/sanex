"""Tests for bounded technical-log snapshots."""

from pathlib import Path

from sanex.sync.log import TechnicalLog


def test_reads_complete_small_log_and_missing_log(tmp_path: Path) -> None:
    path = tmp_path / "sanex.log"
    assert TechnicalLog(path, max_size=8).tail() == b""

    path.write_bytes(b"one\ntwo\n")

    assert TechnicalLog(path, max_size=8).tail() == b"one\ntwo\n"


def test_discards_partial_first_line_from_bounded_tail(tmp_path: Path) -> None:
    path = tmp_path / "sanex.log"
    path.write_bytes("начало\nline-two\nline-three\n".encode())

    tail = TechnicalLog(path, max_size=20).tail()

    assert tail == b"line-two\nline-three\n"
    assert len(tail) <= 20
