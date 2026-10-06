"""Shared assertions for application model contracts."""

import re
from collections.abc import Callable
from typing import TypeVar

import pytest

from sanex.exceptions import ModelError, SaneaException


ErrorT = TypeVar("ErrorT", bound=ModelError)


def assert_invalid_model(
    decoder: Callable[[object], object],
    error_type: type[ErrorT],
    payload: object,
    message: str,
    path: str | None = None,
) -> ErrorT:
    """Assert that a decoded payload is rejected through the public hierarchy."""

    with pytest.raises(error_type, match=re.escape(message)) as raised:
        decoder(payload)

    assert isinstance(raised.value, SaneaException)

    if path is not None:
        assert raised.value.path == path

    return raised.value
