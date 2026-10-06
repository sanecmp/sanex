"""Validated messages exchanged with the desktop indicator."""

from pydantic import BaseModel, ConfigDict

from ..exceptions import IndicatorProtocolError
from .validation import NonEmptyString, NonNegativeInt, parse_json_model


class IndicatorRequest(BaseModel):
    """One request binding a connection to a graphical login session."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sess_ident: NonEmptyString

    def encode(self) -> bytes:
        """Encode one newline-delimited request message."""
        return f"{self.model_dump_json()}\n".encode()


class IndicatorStatus(BaseModel):
    """Minimal session-limit state required by the desktop indicator."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    remaining: NonNegativeInt | None
    break_duration: NonNegativeInt

    @property
    def limited(self) -> bool:
        """Whether the current account is subject to limits."""
        return self.remaining is not None

    def encode(self) -> bytes:
        """Encode one newline-delimited status message."""
        return f"{self.model_dump_json()}\n".encode()


def parse_indicator_request(data: str | bytes | bytearray) -> IndicatorRequest:
    """Decode one strict indicator request."""
    return parse_json_model(IndicatorRequest, data, IndicatorProtocolError)


def parse_indicator_status(data: str | bytes | bytearray) -> IndicatorStatus:
    """Decode one strict indicator status message."""
    return parse_json_model(IndicatorStatus, data, IndicatorProtocolError)
