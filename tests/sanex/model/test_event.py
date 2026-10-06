"""Tests for the sanex event-protocol adapter."""

import pytest

from sanex.exceptions import EventError, SaneaException
from sanex.model.event import iterate_event_records


def test_invalid_shared_event_uses_application_exception() -> None:

    with pytest.raises(EventError, match="discriminator"):
        tuple(iterate_event_records(b"{}\n"))

    assert issubclass(EventError, SaneaException)
