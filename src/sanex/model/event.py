"""Sanex adapters for the shared event wire contract."""

from collections.abc import Iterator

from sanelib import protocol as shared_protocol
from sanelib.exceptions import ProtocolError

from ..exceptions import EventError


def iterate_event_records(
    data: bytes,
    *,
    allow_empty: bool = False,
) -> Iterator[shared_protocol.Event]:
    """Yield a shared JSONL packet using sanex exceptions."""
    try:
        yield from shared_protocol.iterate_event_records(data, allow_empty=allow_empty)

    except ProtocolError as error:
        raise EventError(error.path, error.detail) from error


def validate_event_packet_digest(data: bytes, expected_sha256: str) -> str:
    """Validate an event packet identity using sanex exceptions."""
    try:
        return shared_protocol.validate_event_packet_digest(data, expected_sha256)

    except ProtocolError as error:
        raise EventError(error.path, error.detail) from error
