"""Strict local protocol messages for the per-session window agent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ..exceptions import WindowAgentError
from .validation import NonEmptyString, NonNegativeInt
from .window import AtspiWindow, AtspiWindowSnapshot


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WindowsRequest(_Message):
    ident: NonNegativeInt
    op: Literal["windows"]
    sess_ident: NonEmptyString


class CloseRequest(_Message):
    ident: NonNegativeInt
    op: Literal["close"]
    sess_ident: NonEmptyString
    window: AtspiWindow


Request = Annotated[WindowsRequest | CloseRequest, Field(discriminator="op")]
REQUEST_ADAPTER = TypeAdapter(Request)


class WindowsResponse(_Message):
    ident: NonNegativeInt
    op: Literal["windows"]
    snapshot: AtspiWindowSnapshot


class CloseResponse(_Message):
    ident: NonNegativeInt
    op: Literal["close"]
    accepted: bool


class ErrorResponse(_Message):
    ident: NonNegativeInt
    op: Literal["error"]
    detail: NonEmptyString


Response = Annotated[
    WindowsResponse | CloseResponse | ErrorResponse,
    Field(discriminator="op"),
]
RESPONSE_ADAPTER = TypeAdapter(Response)


def encode_request(request: Request) -> bytes:
    """Serialize one root-to-agent request as compact JSON."""
    return REQUEST_ADAPTER.dump_json(request)


def decode_request(data: bytes) -> Request:
    """Validate one root-to-agent request."""
    try:
        return REQUEST_ADAPTER.validate_json(data, strict=True)

    except (ValidationError, ValueError) as error:
        raise WindowAgentError("decode request", error) from error


def encode_response(response: Response) -> bytes:
    """Serialize one agent-to-root response as compact JSON."""
    return RESPONSE_ADAPTER.dump_json(response)


def decode_response(data: bytes) -> Response:
    """Validate one agent-to-root response."""
    try:
        return RESPONSE_ADAPTER.validate_json(data, strict=True)

    except (ValidationError, ValueError) as error:
        raise WindowAgentError("decode response", error) from error
