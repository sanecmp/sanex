"""Tests for append-only event journals and pending packets."""

import hashlib
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from sanelib.protocol import Event, encode_event

from sanex.exceptions import EventPacketError, EventSequenceError, StorageError
from sanex.storage.events import EventPacket, EventStore


@pytest.fixture
def pending_packet(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> tuple[EventStore, EventPacket]:
    """Create one valid packet waiting for acknowledgement."""
    store = EventStore(tmp_path / "accounts")
    store.append(1001, event_samples[0])
    packet = store.seal(1001)
    assert packet is not None
    return store, packet


def test_manual_seal_preserves_exact_bytes_and_creates_new_open_file(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    store = EventStore(tmp_path / "accounts")
    event = event_samples[0]

    assert store.seal(1001) is None
    assert store.append(1001, event) is None
    packet = store.seal(1001)

    assert packet is not None
    expected = encode_event(event)
    assert packet.path.read_bytes() == expected
    assert packet.sha256 == hashlib.sha256(expected).hexdigest()
    assert packet.path.name == f"{event.seq:020d}-{event.seq:020d}-{packet.sha256}.jsonl"
    assert store.open_path(1001).read_bytes() == b""
    assert stat.S_IMODE(packet.path.stat().st_mode) == 0o600

    for path in (store.events_path(1001), store.pending_path(1001), store.corrupt_path(1001)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_size_limit_seals_only_the_matching_uid(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    store = EventStore(tmp_path / "accounts", max_open_events=2)

    assert store.append(1001, event_samples[0]) is None
    assert store.append(1002, event_samples[0]) is None
    packet = store.append(1001, event_samples[1])

    assert packet is not None
    assert packet.uid == 1001
    assert packet.first_seq == event_samples[0].seq
    assert packet.last_seq == event_samples[1].seq
    assert store.open_path(1001).read_bytes() == b""
    assert store.open_path(1002).read_bytes() == encode_event(event_samples[0])


def test_byte_limit_seals_before_event_that_would_exceed_it(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    first_data = encode_event(event_samples[0])
    second_data = encode_event(event_samples[1])
    store = EventStore(
        tmp_path / "accounts",
        max_open_bytes=len(first_data) + len(second_data) - 1,
    )

    assert store.append(1001, event_samples[0]) is None
    packet = store.append(1001, event_samples[1])

    assert packet is not None
    assert packet.path.read_bytes() == first_data
    assert store.open_path(1001).read_bytes() == second_data


def test_existing_open_journal_is_continued_after_restart(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    accounts_root = tmp_path / "accounts"
    first_store = EventStore(accounts_root)
    first_store.append(1001, event_samples[0])
    first_store.append(1001, event_samples[1])
    restarted_store = EventStore(accounts_root)

    restarted_store.append(1001, event_samples[2])
    packet = restarted_store.seal(1001)

    assert packet is not None
    assert packet.first_seq == event_samples[0].seq
    assert packet.last_seq == event_samples[2].seq
    assert packet.path.read_bytes() == b"".join(encode_event(event) for event in event_samples[:3])


def test_finds_only_uids_with_stored_event_data(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    accounts_root = tmp_path / "accounts"
    store = EventStore(accounts_root)

    assert store.find_stored_uids() == ()

    store.append(1002, event_samples[0])
    store.recover(1001, 0)
    (accounts_root / "not-a-uid").mkdir()

    assert EventStore(accounts_root).find_stored_uids() == (1002,)


def test_reused_sequence_is_rejected_before_append(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    store = EventStore(tmp_path / "accounts")
    store.append(1001, event_samples[0])
    path = store.open_path(1001)
    original = path.read_bytes()

    with pytest.raises(EventSequenceError, match="must be greater"):
        store.append(1001, event_samples[0])

    assert path.read_bytes() == original


def test_failed_atomic_move_preserves_open_journal(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EventStore(tmp_path / "accounts")
    store.append(1001, event_samples[0])
    open_path = store.open_path(1001)
    original = open_path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("sanex.storage.atomic.os.replace", fail_replace)

    with pytest.raises(StorageError, match="replace failed"):
        store.seal(1001)

    assert open_path.read_bytes() == original
    assert list(store.pending_path(1001).iterdir()) == []


def test_recovery_truncates_incomplete_final_record_and_uses_highest_seq(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
) -> None:
    accounts_root = tmp_path / "accounts"
    store = EventStore(accounts_root)
    store.append(1001, event_samples[0])
    open_path = store.open_path(1001)
    complete = open_path.read_bytes()

    with open_path.open("ab") as stream:
        stream.write(b"{\"seq\":")

    restarted = EventStore(accounts_root)
    assert restarted.recover(1001, 100) == event_samples[0].seq
    assert open_path.read_bytes() == complete
    assert restarted.recover(1001, 500) == 500


def test_recovery_quarantines_open_with_an_invalid_complete_record(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    accounts_root = tmp_path / "accounts"
    store = EventStore(accounts_root)
    store.append(1001, event_samples[0])
    open_path = store.open_path(1001)
    corrupted = open_path.read_bytes() + b"{}\n" + encode_event(event_samples[1])
    open_path.write_bytes(corrupted)

    restarted = EventStore(accounts_root)

    with caplog.at_level("ERROR"):
        recovered_seq = restarted.recover(1001, 100)

    quarantined = tuple(restarted.corrupt_path(1001).iterdir())
    assert recovered_seq == event_samples[0].seq
    assert restarted.open_path(1001).read_bytes() == b""
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupted
    assert "Quarantined corrupt event file open.jsonl" in caplog.text


def test_recovery_keeps_valid_pending_and_quarantines_invalid_packets(
    tmp_path: Path,
    event_samples: tuple[Event, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    accounts_root = tmp_path / "accounts"
    store = EventStore(accounts_root)
    store.append(1001, event_samples[0])
    valid_packet = store.seal(1001)
    assert valid_packet is not None
    data = valid_packet.path.read_bytes()
    pending_path = store.pending_path(1001)
    first_seq = event_samples[0].seq
    wrong_hash = pending_path / f"{first_seq:020d}-{first_seq:020d}-{"0" * 64}.jsonl"
    wrong_range = pending_path / f"{first_seq:020d}-{999:020d}-{valid_packet.sha256}.jsonl"
    invalid_name = pending_path / "invalid.jsonl"

    for path in (wrong_hash, wrong_range, invalid_name):
        path.write_bytes(data)

    store.open_path(1001).unlink()

    restarted = EventStore(accounts_root)

    with caplog.at_level("ERROR"):
        recovered_seq = restarted.recover(1001, 0)

    assert recovered_seq == 999
    assert restarted.pending(1001) == (valid_packet,)
    assert restarted.open_path(1001).read_bytes() == b""
    assert len(tuple(restarted.corrupt_path(1001).iterdir())) == 3
    assert caplog.text.count("Quarantined corrupt event file") == 3


def test_acknowledge_durably_removes_known_packet(
    pending_packet: tuple[EventStore, EventPacket],
) -> None:
    store, packet = pending_packet

    store.acknowledge(packet)

    assert not packet.path.exists()
    assert store.pending(packet.uid) == ()
    assert EventStore(store.accounts_root).pending(packet.uid) == ()


def test_acknowledge_rejects_unknown_packet(
    pending_packet: tuple[EventStore, EventPacket],
) -> None:
    store, packet = pending_packet
    unknown = replace(packet, sha256="0" * 64)

    with pytest.raises(EventPacketError, match="packet is not pending"):
        store.acknowledge(unknown)

    assert packet.path.exists()
    assert store.pending(packet.uid) == (packet,)


def test_acknowledge_rejects_changed_packet(
    pending_packet: tuple[EventStore, EventPacket],
) -> None:
    store, packet = pending_packet
    packet.path.write_bytes(packet.path.read_bytes() + b" ")

    with pytest.raises(EventPacketError, match="changed before acknowledgement"):
        store.acknowledge(packet)

    assert packet.path.exists()
    assert store.pending(packet.uid) == (packet,)


def test_acknowledge_preserves_packet_when_removal_fails(
    pending_packet: tuple[EventStore, EventPacket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, packet = pending_packet
    original_unlink = Path.unlink

    def fail_packet_unlink(path: Path, missing_ok: bool = False) -> None:

        if path == packet.path:
            raise OSError("unlink failed")

        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_packet_unlink)

    with pytest.raises(StorageError, match="unlink failed"):
        store.acknowledge(packet)

    assert packet.path.exists()
    assert store.pending(packet.uid) == (packet,)
