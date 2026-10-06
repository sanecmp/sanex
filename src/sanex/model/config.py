"""Sanex adapter for the shared configuration wire contract."""

from sanelib import protocol as shared_protocol
from sanelib.exceptions import ProtocolError

from ..exceptions import ConfigError


def parse_config(data: str | bytes | bytearray) -> shared_protocol.Config:
    """Decode shared wire data into a config using sanex exceptions."""
    try:
        return shared_protocol.parse_config(data)

    except ProtocolError as error:
        raise ConfigError(error.path, error.detail) from error
