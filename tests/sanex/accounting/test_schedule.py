"""Tests for local configured-range selection."""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sanelib.protocol import Limits, decode_config

from sanex.accounting.schedule import ScheduleResolver
from sanex.exceptions import AccountingError


ZONE = ZoneInfo("Asia/Novosibirsk")


def unix_timestamp(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> int:
    """Build a Unix timestamp from one local fixture moment."""
    return int(datetime(year, month, day, hour, minute, second, tzinfo=ZONE).timestamp())


def configured_schedule(config_payload: dict[str, Any]) -> tuple[ScheduleResolver, Limits]:
    """Return the resolver and limits from the shared configuration fixture."""
    config = decode_config(config_payload)
    limits = config.accounts[0].limits
    assert limits is not None
    return ScheduleResolver.for_config(config), limits


def test_selects_range_and_rule_in_configured_timezone(config_payload: dict[str, Any]) -> None:
    resolver, limits = configured_schedule(config_payload)

    selected = resolver.resolve(limits, unix_timestamp(2026, 10, 2, 22, 0))

    assert selected is not None
    assert selected.occurrence_date == date(2026, 10, 2)
    assert selected.range.ident == 501
    assert selected.session_rule.ident == 601


@pytest.mark.parametrize(
    "timestamp",
    [
        unix_timestamp(2026, 10, 2, 21, 59, 59),
        unix_timestamp(2026, 10, 3, 0, 0),
    ],
)
def test_range_boundaries_are_half_open(
    config_payload: dict[str, Any],
    timestamp: int,
) -> None:
    resolver, limits = configured_schedule(config_payload)

    assert resolver.resolve(limits, timestamp) is None


def test_disabled_range_is_not_selected(config_payload: dict[str, Any]) -> None:
    config_payload["accounts"][0]["limits"]["ranges"][0]["apply"] = False
    resolver, limits = configured_schedule(config_payload)

    assert resolver.resolve(limits, unix_timestamp(2026, 10, 2, 23, 0)) is None


def test_invalid_timestamp_is_rejected(config_payload: dict[str, Any]) -> None:
    resolver, limits = configured_schedule(config_payload)

    with pytest.raises(AccountingError, match="range timestamp is invalid"):
        resolver.resolve(limits, -1)
