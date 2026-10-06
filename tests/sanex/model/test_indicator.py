"""Tests for the local desktop-indicator protocol models."""

import pytest

from sanex.exceptions import IndicatorProtocolError
from sanex.model.indicator import (
    IndicatorStatus,
    parse_indicator_request,
    parse_indicator_status,
)


def test_parses_strict_request_and_encodes_compact_status() -> None:
    request = parse_indicator_request(b"{\"sess_ident\":\"3\"}\n")
    status = IndicatorStatus(remaining=3600, break_duration=7200)

    assert request.sess_ident == "3"
    assert request.encode() == b"{\"sess_ident\":\"3\"}\n"
    assert status.encode() == b"{\"remaining\":3600,\"break_duration\":7200}\n"
    assert parse_indicator_status(status.encode()) == status


def test_rejects_unknown_request_fields() -> None:

    with pytest.raises(IndicatorProtocolError, match="Extra inputs are not permitted"):
        parse_indicator_request(b"{\"sess_ident\":\"3\",\"op\":\"write\"}\n")
