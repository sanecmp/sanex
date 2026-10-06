"""Shared validation helpers for JSON-backed application models."""

import json
from collections.abc import Iterable
from json import JSONDecodeError
from typing import Annotated, TypeVar

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from ..exceptions import ModelError, SaneaException


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
    _check_json_object_keys(data, error_type)
    try:
        return adapter.validate_json(data, strict=True)

    except ValidationError as error:
        raise _model_error(error, error_type) from error


def decode_json_adapter(
    adapter: TypeAdapter[ValueT],
    value: object,
    error_type: type[ErrorT],
) -> ValueT:
    """Validate a JSON-compatible Python value with a type adapter."""
    try:
        data = json.dumps(value, allow_nan=False, ensure_ascii=False)

    except (TypeError, ValueError) as error:
        raise error_type("$", f"value is not JSON-compatible: {error}") from error

    return parse_json_adapter(adapter, data, error_type)


def require_unique(values: Iterable[object], name: str) -> None:
    """Require unique values from an iterable."""
    seen: set[object] = set()

    for value in values:

        if value in seen:
            raise ValueError(f"duplicate {name} {value!r}")

        seen.add(value)


def _check_json_object_keys(data: str | bytes | bytearray, error_type: type[ErrorT]) -> None:
    try:
        json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )

    except (JSONDecodeError, UnicodeDecodeError) as error:
        raise error_type("$", f"invalid JSON: {error}") from error

    except _DuplicateKeyError as error:
        raise error_type("$", f"duplicate object key {error.key!r}") from error

    except _InvalidConstantError as error:
        raise error_type("$", f"invalid JSON constant {error.value}") from error


def _model_error(error: ValidationError, error_type: type[ErrorT]) -> ErrorT:
    first = error.errors(include_url=False, include_input=False)[0]
    path = _error_path(first["loc"])
    message = f"{first["msg"]}"

    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")

    return error_type(path, message)


def _error_path(location: tuple[int | str, ...]) -> str:
    chunks = ["$"]

    for item in location:
        chunks.append(f"[{item}]" if isinstance(item, int) else f".{item}")

    return "".join(chunks)


class _DuplicateKeyError(SaneaException):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _InvalidConstantError(SaneaException):
    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}

    for key, value in pairs:

        if key in result:
            raise _DuplicateKeyError(key)

        result[key] = value

    return result


def _reject_json_constant(value: str) -> object:
    raise _InvalidConstantError(value)
