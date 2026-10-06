"""Tests for range-occurrence runtime-state access."""

from datetime import date
from typing import Any

from sanelib.protocol import decode_config

from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.schedule import ActiveRange
from sanex.model.state import decode_runtime_state


def active_range(config_payload: dict[str, Any], occurrence_date: date) -> ActiveRange:
    """Build a selected range from the shared configuration fixture."""
    config = decode_config(config_payload)
    limits = config.accounts[0].limits
    assert limits is not None
    return ActiveRange(
        occurrence_date=occurrence_date,
        range=limits.ranges[0],
        session_rule=limits.session_rules[0],
    )


def test_finds_existing_occurrence_without_mutating_state(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    selected = active_range(config_payload, date(2026, 10, 1))
    existing = state.occurrences[0]

    runtime = AccountRuntime(1001, state)

    assert runtime.find_occurrence(501, date(2026, 10, 1)) is existing
    assert runtime.ensure_occurrence(selected) is existing
    assert state.occurrences == [existing]


def test_creates_each_occurrence_once(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    selected = active_range(config_payload, date(2026, 10, 2))

    runtime = AccountRuntime(1001, state)

    created = runtime.ensure_occurrence(selected)

    assert created.range_ident == 501
    assert created.date == date(2026, 10, 2)
    assert created.sessions == []
    assert created.apps == []
    assert runtime.ensure_occurrence(selected) is created
    assert len(state.occurrences) == 2


def test_discards_only_completed_occurrences_from_earlier_dates(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    completed = state.occurrences[0]
    active = completed.model_copy(deep=True)
    active.range_ident = 502
    completed.sessions[0].active = False
    completed.apps[0].active_wnds = []
    state.occurrences.append(active)
    selected = active_range(config_payload, date(2026, 10, 2))

    runtime = AccountRuntime(1001, state)
    created = runtime.ensure_occurrence(selected)

    assert state.occurrences == [active, created]
