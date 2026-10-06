"""Tests for user-visible session status calculation."""

from typing import Any

from sanelib.protocol import decode_config

from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.schedule import ScheduleResolver
from sanex.indicator.status import IndicatorStatusBuilder
from sanex.model.indicator import IndicatorStatus
from sanex.model.state import decode_runtime_state


ACTIVE_TIMESTAMP = 1_790_956_800


def build_status(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
    timestamp: int = ACTIVE_TIMESTAMP,
) -> IndicatorStatus:
    """Build status from aligned configuration and runtime fixtures."""
    runtime_payload["occurrences"][0]["date"] = "2026-10-02"
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    builder = IndicatorStatusBuilder(
        AccountRuntime(1001, state),
        ScheduleResolver.for_config(config),
    )
    return builder.build(config.accounts[0], "3", timestamp)


def test_uses_nearest_session_or_schedule_limit(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    status = build_status(config_payload, runtime_payload)

    assert status.limited
    assert status.remaining == 3600
    assert status.break_duration == 7200


def test_uses_remaining_session_quota_when_it_is_nearer(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["max_duration"] = 900
    status = build_status(config_payload, runtime_payload)

    assert status.remaining == 88


def test_disabled_account_is_unlimited(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["apply"] = False
    status = build_status(config_payload, runtime_payload)

    assert not status.limited
    assert status.remaining is None
    assert status.break_duration == 0


def test_disabled_session_rule_only_uses_schedule_boundary(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["apply"] = False
    status = build_status(config_payload, runtime_payload)

    assert status.remaining == 3600
    assert status.break_duration == 0


def test_outside_schedule_has_no_remaining_time_or_break(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    status = build_status(config_payload, runtime_payload, ACTIVE_TIMESTAMP + 3600)

    assert status.remaining == 0
    assert status.break_duration == 0
