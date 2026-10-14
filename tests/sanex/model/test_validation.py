"""Tests for application errors around shared JSON validation."""

from collections.abc import Callable

import pytest
from pydantic import TypeAdapter

from sanex.exceptions import RuntimeStateError
from sanex.model.state import RuntimeState
from sanex.model.validation import (
    decode_json_adapter,
    decode_json_model,
    parse_json_adapter,
)


@pytest.mark.parametrize(
    ("decoder", "payload"),
    [
        pytest.param(parse_json_adapter, b"[true]", id="json"),
        pytest.param(decode_json_adapter, [True], id="python"),
    ],
)
def test_type_adapters_preserve_application_errors_and_paths(
    decoder: Callable[..., object],
    payload: object,
) -> None:
    adapter = TypeAdapter(list[int])

    with pytest.raises(RuntimeStateError, match=r"\$\[0\]: Input should be a valid integer") as raised:
        decoder(adapter, payload, RuntimeStateError)

    assert raised.value.path == "$[0]"


@pytest.mark.parametrize(
    ("decoder", "schema"),
    [
        pytest.param(decode_json_model, RuntimeState, id="model"),
        pytest.param(decode_json_adapter, TypeAdapter(RuntimeState), id="adapter"),
    ],
)
def test_json_decoders_reject_unserializable_values_with_application_errors(
    decoder: Callable[..., object],
    schema: type[RuntimeState] | TypeAdapter[RuntimeState],
) -> None:

    with pytest.raises(RuntimeStateError, match=r"\$: value is not JSON-compatible"):
        decoder(schema, object(), RuntimeStateError)
