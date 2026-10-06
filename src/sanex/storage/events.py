"""Append-only per-account event journals and immutable pending packets."""

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from sanelib.protocol import Event, MAX_EVENTS_PER_PACKET, MAX_EVENT_PACKET_SIZE, encode_event

from ..exceptions import EventError, EventPacketError, EventSequenceError, StorageError
from ..model.event import iterate_event_records, validate_event_packet_digest
from .atomic import (
    DEFAULT_FILE_MODE,
    atomic_move,
    atomic_write_bytes,
    ensure_private_directory,
    fsync_directory,
)
from .paths import DEFAULT_ACCOUNTS_STATE_ROOT


_PACKET_NAME = re.compile(r"(?P<first>[0-9]{20})-(?P<last>[0-9]{20})-(?P<sha256>[0-9a-f]{64})[.]jsonl")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventPacket:
    """One immutable event packet ready for synchronization."""

    uid: int
    path: Path
    first_seq: int
    last_seq: int
    sha256: str


@dataclass(slots=True)
class _OpenMetadata:
    count: int = 0
    size: int = 0
    first_seq: int | None = None
    last_seq: int | None = None


@dataclass(slots=True)
class EventStore:
    """Own active JSONL journals and seal them into immutable packets."""

    accounts_root: Path = DEFAULT_ACCOUNTS_STATE_ROOT
    max_open_events: int = MAX_EVENTS_PER_PACKET
    max_open_bytes: int = MAX_EVENT_PACKET_SIZE
    _open_metadata: dict[int, _OpenMetadata] = field(default_factory=dict, init=False, repr=False)
    _pending_packets: dict[int, tuple[EventPacket, ...]] = field(default_factory=dict, init=False, repr=False)
    _event_seq: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:

        if self.max_open_events <= 0:
            raise ValueError("max_open_events must be positive")

        if self.max_open_bytes <= 0:
            raise ValueError("max_open_bytes must be positive")

    def recover(self, uid: int, runtime_event_seq: int) -> int:
        """Repair one UID journal, validate pending packets and return the sequence watermark."""
        self._prepare_directories(uid)
        metadata, observed_open_seq = self._recover_open(uid)
        packets, pending_seq = self._recover_pending(uid)
        event_seq = max(runtime_event_seq, observed_open_seq, pending_seq)
        self._open_metadata[uid] = metadata
        self._pending_packets[uid] = packets
        self._event_seq[uid] = event_seq
        return event_seq

    def append(self, uid: int, event: Event) -> EventPacket | None:
        """Append one event and seal the journal when its size limit is reached."""
        metadata = self._metadata(uid)
        previous = self._event_seq[uid]

        if event.seq <= previous:
            raise EventSequenceError(uid, previous, event.seq)

        data = encode_event(event)
        sealed_packet = None

        if metadata.count and metadata.size + len(data) > self.max_open_bytes:
            sealed_packet = self.seal(uid)
            metadata = self._metadata(uid)

        path = self.open_path(uid)
        try:

            with path.open("ab") as stream:
                stream.write(data)

        except OSError as error:
            raise StorageError("append", path, error) from error

        metadata.count += 1
        metadata.size += len(data)

        if metadata.first_seq is None:
            metadata.first_seq = event.seq

        metadata.last_seq = event.seq
        self._event_seq[uid] = event.seq

        if (
            sealed_packet is None
            and (
                metadata.count >= self.max_open_events
                or metadata.size >= self.max_open_bytes
            )
        ):
            return self.seal(uid)

        return sealed_packet

    def seal(self, uid: int) -> EventPacket | None:
        """Synchronize and atomically move a non-empty journal into pending."""
        metadata = self._metadata(uid)

        if metadata.count == 0:
            return None

        if metadata.first_seq is None or metadata.last_seq is None:
            raise EventPacketError("open event metadata is incomplete")

        open_path = self.open_path(uid)
        try:

            with open_path.open("rb") as stream:
                data = stream.read()
                os.fsync(stream.fileno())

        except OSError as error:
            raise StorageError("read and synchronize", open_path, error) from error

        digest = hashlib.sha256(data).hexdigest()
        filename = f"{metadata.first_seq:020d}-{metadata.last_seq:020d}-{digest}.jsonl"
        pending_path = self.pending_path(uid) / filename

        if pending_path.exists():
            raise EventPacketError(f"pending packet already exists: {filename}")

        atomic_move(open_path, pending_path)
        self._create_open_file(uid)
        packet = EventPacket(uid, pending_path, metadata.first_seq, metadata.last_seq, digest)
        self._open_metadata[uid] = _OpenMetadata()
        self._pending_packets[uid] = tuple(sorted((*self._pending_packets[uid], packet), key=lambda item: item.first_seq))
        return packet

    def pending(self, uid: int) -> tuple[EventPacket, ...]:
        """Return validated immutable packets waiting for synchronization."""
        self._metadata(uid)
        return self._pending_packets[uid]

    def find_stored_uids(self) -> tuple[int, ...]:
        """Return UIDs with event data still waiting in the filesystem."""
        root = self.accounts_root
        try:
            account_paths = tuple(root.iterdir())

        except FileNotFoundError:
            return ()

        except OSError as error:
            raise StorageError("list", root, error) from error

        uids: list[int] = []

        for account_path in account_paths:
            name = account_path.name

            if not name.isascii() or not name.isdecimal():
                continue

            uid = int(name)
            events_path = account_path / "events"
            open_path = events_path / "open.jsonl"
            pending_path = events_path / "pending"
            try:
                has_open_events = open_path.is_file() and open_path.stat().st_size > 0
                has_pending_entries = pending_path.is_dir() and any(pending_path.iterdir())

            except OSError as error:
                raise StorageError("inspect", events_path, error) from error

            if has_open_events or has_pending_entries:
                uids.append(uid)

        return tuple(sorted(uids))

    def acknowledge(self, packet: EventPacket) -> None:
        """Durably remove one known packet after its remote acknowledgement."""
        known_packets = self.pending(packet.uid)

        if packet not in known_packets:
            raise EventPacketError(f"packet is not pending: {packet.sha256}")

        try:
            data = packet.path.read_bytes()

        except OSError as error:
            raise StorageError("read", packet.path, error) from error

        if hashlib.sha256(data).hexdigest() != packet.sha256:
            raise EventPacketError(f"pending packet changed before acknowledgement: {packet.sha256}")

        try:
            packet.path.unlink()

        except OSError as error:
            raise StorageError("remove", packet.path, error) from error

        self._pending_packets[packet.uid] = tuple(known for known in known_packets if known != packet)
        try:
            fsync_directory(packet.path.parent)

        except OSError as error:
            raise StorageError("synchronize directory", packet.path.parent, error) from error

    def open_path(self, uid: int) -> Path:
        """Return the active JSONL journal path for one UID."""
        return self.events_path(uid) / "open.jsonl"

    def pending_path(self, uid: int) -> Path:
        """Return the immutable pending-packet directory for one UID."""
        return self.events_path(uid) / "pending"

    def corrupt_path(self, uid: int) -> Path:
        """Return the quarantine directory for one UID."""
        return self.events_path(uid) / "corrupt"

    def events_path(self, uid: int) -> Path:
        """Return the event-storage directory for one UID."""
        return self.accounts_root / f"{uid}" / "events"

    def _metadata(self, uid: int) -> _OpenMetadata:
        metadata = self._open_metadata.get(uid)

        if metadata is None:
            self.recover(uid, 0)
            metadata = self._open_metadata[uid]

        return metadata

    def _prepare_directories(self, uid: int) -> None:
        ensure_private_directory(self.events_path(uid))
        ensure_private_directory(self.pending_path(uid))
        ensure_private_directory(self.corrupt_path(uid))

    def _create_open_file(self, uid: int) -> None:
        path = self.open_path(uid)
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                DEFAULT_FILE_MODE,
            )
            os.fchmod(descriptor, DEFAULT_FILE_MODE)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            fsync_directory(path.parent)

        except OSError as error:
            raise StorageError("create", path, error) from error

        finally:

            if descriptor >= 0:
                os.close(descriptor)

    def _recover_open(self, uid: int) -> tuple[_OpenMetadata, int]:
        path = self.open_path(uid)

        if not path.exists():
            self._create_open_file(uid)
            return _OpenMetadata(), 0

        try:
            path.chmod(DEFAULT_FILE_MODE)
            data = path.read_bytes()

        except OSError as error:
            raise StorageError("read", path, error) from error

        if not data:
            return _OpenMetadata(), 0

        complete_data = data

        if not data.endswith(b"\n"):
            final_lf = data.rfind(b"\n")
            complete_data = data[: final_lf + 1] if final_lf >= 0 else b""

        metadata = _OpenMetadata()
        try:
            self._read_event_metadata(complete_data, metadata)

        except (EventError, EventSequenceError) as error:
            observed_seq = metadata.last_seq or 0
            self._quarantine(uid, path, error)
            self._create_open_file(uid)
            return _OpenMetadata(), observed_seq

        if complete_data != data:
            atomic_write_bytes(path, complete_data)

        return metadata, metadata.last_seq or 0

    def _recover_pending(self, uid: int) -> tuple[tuple[EventPacket, ...], int]:
        packets: list[EventPacket] = []
        pending_seq = 0
        try:
            entries = sorted(self.pending_path(uid).iterdir(), key=lambda path: path.name)

        except OSError as error:
            raise StorageError("list", self.pending_path(uid), error) from error

        for path in entries:
            name_match = _PACKET_NAME.fullmatch(path.name)

            if name_match is not None:
                pending_seq = max(pending_seq, int(name_match.group("last")))

            try:
                packets.append(self._read_packet(uid, path))

            except (EventError, EventPacketError, EventSequenceError) as error:
                self._quarantine(uid, path, error)

        ordered = tuple(sorted(packets, key=lambda packet: packet.first_seq))
        return ordered, pending_seq

    def _read_packet(self, uid: int, path: Path) -> EventPacket:

        if not path.is_file():
            raise EventPacketError(f"pending entry is not a file: {path.name}")

        match = _PACKET_NAME.fullmatch(path.name)

        if match is None:
            raise EventPacketError(f"invalid pending packet name: {path.name}")

        try:
            path.chmod(DEFAULT_FILE_MODE)
            data = path.read_bytes()

        except OSError as error:
            raise StorageError("read", path, error) from error

        if not data or not data.endswith(b"\n"):
            raise EventPacketError("pending packet is empty or has an incomplete final record")

        metadata = self._read_event_metadata(data)
        first_seq = int(match.group("first"))
        last_seq = int(match.group("last"))
        digest = match.group("sha256")

        if metadata.first_seq != first_seq or metadata.last_seq != last_seq:
            raise EventPacketError("pending packet sequence range does not match its name")

        validate_event_packet_digest(data, digest)
        return EventPacket(uid, path, first_seq, last_seq, digest)

    @staticmethod
    def _read_event_metadata(
        data: bytes,
        metadata: _OpenMetadata | None = None,
    ) -> _OpenMetadata:

        if metadata is None:
            metadata = _OpenMetadata()

        for event in iterate_event_records(data, allow_empty=True):
            metadata.count += 1

            if metadata.first_seq is None:
                metadata.first_seq = event.seq

            metadata.last_seq = event.seq

        metadata.size = len(data)
        return metadata

    def _quarantine(self, uid: int, path: Path, error: Exception) -> None:
        digest = self._path_digest(path)
        stem = f"{path.name}-{digest}"
        target = self.corrupt_path(uid) / stem
        suffix = 1

        while target.exists():
            target = self.corrupt_path(uid) / f"{stem}-{suffix}"
            suffix += 1

        atomic_move(path, target)
        logger.error("Quarantined corrupt event file %s: %s", path.name, error)

    @staticmethod
    def _path_digest(path: Path) -> str:

        if not path.is_file():
            return "invalid-entry"

        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        except OSError as error:
            raise StorageError("read", path, error) from error
