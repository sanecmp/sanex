"""Sanex exceptions and constraints around the shared JSON validation."""

from typing import Annotated, TypeVar

from pydantic import BaseModel, Field, TypeAdapter
from sanelib.exceptions import ProtocolError
from sanelib.utils.validation import (
    decode_json_adapter as decode_shared_adapter,
    parse_json_adapter as parse_shared_adapter,
    require_unique as require_unique,
)

from ..exceptions import ModelError


NonNegativeInt = Annotated[int, Field(ge=0)]
NonEmptyString = Annotated[str, Field(min_length=1)]

ModelT = TypeVar("ModelT", bound=BaseModel)
ValueT = TypeVar("ValueT")
ErrorT = TypeVar("ErrorT", bound=ModelError)


def parse_json_model(
    model_type: type[ModelT],
    data: str | bytes | bytearray,
    error_type: type[ErrorT],
) -> ModelT:
    """Decode JSON and validate it against a Pydantic model."""
    return parse_json_adapter(TypeAdapter(model_type), data, error_type)


def decode_json_model(
    model_type: type[ModelT],
    value: object,
    error_type: type[ErrorT],
) -> ModelT:
    """Validate a JSON-compatible Python value against a Pydantic model."""
    return decode_json_adapter(TypeAdapter(model_type), value, error_type)


def parse_json_adapter(
    adapter: TypeAdapter[ValueT],
    data: str | bytes | bytearray,
    error_type: type[ErrorT],
) -> ValueT:
    """Decode JSON and validate it with a reusable Pydantic type adapter."""
    try:
        return parse_shared_adapter(adapter, data)

    except ProtocolError as error:
        raise error_type(error.path, error.detail) from error


def decode_json_adapter(
    adapter: TypeAdapter[ValueT],
    value: object,
    error_type: type[ErrorT],
) -> ValueT:
    """Validate a JSON-compatible Python value with a type adapter."""
    try:
        return decode_shared_adapter(adapter, value)

    except ProtocolError as error:
        raise error_type(error.path, error.detail) from error
