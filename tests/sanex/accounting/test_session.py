"""Tests for per-occurrence login-session quota accounting."""

from typing import Any

import pytest
from sanelib.protocol import SessionRule, decode_config

from sanex.accounting.session import (
    SessionLimit,
    SessionQuota,
)
from sanex.accounting.runtime import AccountRuntime
from sanex.model.state import RangeOccurrence, decode_runtime_state


def occurrence_and_rule(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> tuple[RangeOccurrence, SessionRule]:
    """Load the shared occurrence and its configured rule."""
    config = decode_config(config_payload)
    limits = config.accounts[0].limits
    assert limits is not None
    state = decode_runtime_state(runtime_payload)
    return state.occurrences[0], limits.session_rules[0]


def test_session_is_counted_once(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence, rule = occurrence_and_rule(config_payload, runtime_payload)

    quota = SessionQuota(occurrence, rule)
    first = quota.start("4", 1_790_842_400)
    repeated = quota.start("4", 1_790_842_500)

    assert first.allowed
    assert first.usage is not None
    assert repeated.usage is first.usage
    assert first.usage.started == 1_790_842_400
    assert first.usage.max_duration == 7200
    assert len(occurrence.sessions) == 2


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"max_sessions": 1}, SessionLimit.MAX_SESSIONS),
        ({"max_duration": 59}, SessionLimit.MAX_DURATION),
    ],
)
def test_rejected_session_does_not_consume_count(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
    changes: dict[str, int],
    expected: SessionLimit,
) -> None:
    occurrence, rule = occurrence_and_rule(config_payload, runtime_payload)
    rule = rule.model_copy(update=changes)

    result = SessionQuota(occurrence, rule).start("4", 1_790_842_400)

    assert not result.allowed
    assert result.limit is expected
    assert result.usage is None
    assert len(occurrence.sessions) == 1


def test_duration_limit_is_reached_at_exact_boundary(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence, rule = occurrence_and_rule(config_payload, runtime_payload)
    usage = occurrence.sessions[0]

    limit = SessionQuota(occurrence, rule).spend(usage, 6388)

    assert usage.spent == 7200
    assert limit is SessionLimit.MAX_DURATION


def test_disabled_rule_neither_spends_nor_enforces(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence, rule = occurrence_and_rule(config_payload, runtime_payload)
    rule = rule.model_copy(update={"apply": False, "max_duration": 1})
    usage = occurrence.sessions[0]

    assert SessionQuota(occurrence, rule).spend(usage, 100) is None
    assert usage.spent == 812
    assert usage.max_duration == 1


def test_session_started_while_disabled_counts_only_if_still_running_on_enable(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence, rule = occurrence_and_rule(config_payload, runtime_payload)
    disabled = rule.model_copy(update={"apply": False})

    while_disabled = SessionQuota(occurrence, disabled).start(
        "4", 1_790_842_400
    )
    after_enable = SessionQuota(occurrence, rule).start("4", 1_790_842_500)

    assert while_disabled.allowed
    assert while_disabled.usage is None
    assert after_enable.allowed
    assert after_enable.usage is not None
    assert after_enable.usage.started == 1_790_842_500
    assert len(occurrence.sessions) == 2


def test_forced_finish_starts_break_once(runtime_payload: dict[str, Any]) -> None:
    state = decode_runtime_state(runtime_payload)
    runtime = AccountRuntime(1001, state)
    usage = state.occurrences[0].sessions[0]

    assert runtime.request_session_termination(usage, 1_790_845_000)
    assert not runtime.request_session_termination(usage, 1_790_845_001)
    break_till = runtime.finish_session(usage, 1_790_845_010)

    assert not usage.active
    assert usage.terminate_requested == 1_790_845_000
    assert break_till == 1_790_852_210
    assert runtime.finish_session(usage, 1_790_845_100) == break_till


@pytest.mark.parametrize(
    ("spent", "expected_break"),
    [
        (5760, False),
        (5761, True),
    ],
)
def test_voluntary_break_threshold_is_strictly_greater_than_four_fifths(
    runtime_payload: dict[str, Any],
    spent: int,
    expected_break: bool,
) -> None:
    state = decode_runtime_state(runtime_payload)
    runtime = AccountRuntime(1001, state)
    usage = state.occurrences[0].sessions[0]
    usage.spent = spent

    runtime.finish_session(usage, 1_790_845_010)

    assert (state.break_till is not None) is expected_break


def test_existing_break_is_not_shortened(runtime_payload: dict[str, Any]) -> None:
    state = decode_runtime_state(runtime_payload)
    runtime = AccountRuntime(1001, state)
    state.break_till = 1_790_900_000
    usage = state.occurrences[0].sessions[0]
    runtime.request_session_termination(usage, 1_790_845_000)

    assert runtime.finish_session(usage, 1_790_845_010) == 1_790_900_000
    assert runtime.break_remaining( 1_790_899_900) == 100
    assert runtime.break_remaining( 1_790_900_000) == 0
