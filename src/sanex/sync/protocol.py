"""Sanex adapter for the shared synchronization wire contract."""

from sanelib import protocol as shared_protocol
from sanelib.exceptions import ProtocolError

from ..exceptions import SyncProtocolError


def parse_sync_response(data: str | bytes | bytearray) -> shared_protocol.SyncResponse:
    """Decode a control response using sanex exceptions."""
    try:
        return shared_protocol.parse_sync_response(data)

    except ProtocolError as error:
        raise SyncProtocolError(error.path, error.detail) from error
