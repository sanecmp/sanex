"""Tests for monotonic active-time accounting."""

import pytest

from sanex.accounting.clock import ActiveTimeClock
from sanex.exceptions import AccountingError


def test_whole_seconds_preserve_subsecond_remainder() -> None:
    readings = iter((0, 1_500_000_000, 2_250_000_000))
    clock = ActiveTimeClock(now=readings.__next__)

    assert clock.advance() == 1
    assert clock.advance() == 1


def test_sleep_time_is_excluded_without_losing_active_remainder() -> None:
    readings = iter((0, 2_250_000_000, 102_250_000_000, 103_250_000_000, 104_000_000_000))
    clock = ActiveTimeClock(now=readings.__next__)

    assert clock.set_sleeping(True) == 2
    assert clock.sleeping
    assert clock.advance() == 0
    assert clock.set_sleeping(False) == 0
    assert not clock.sleeping
    assert clock.advance() == 1


def test_invalid_monotonic_clock_is_rejected() -> None:
    readings = iter((10, 9))
    clock = ActiveTimeClock(now=readings.__next__)

    with pytest.raises(AccountingError, match="moved backwards"):
        clock.advance()
