"""Tests for per-occurrence application quota accounting."""

from typing import Any

from sanex.accounting.app import AppQuota
from tests.sanex.accounting.factories import configured_app_rule, runtime_occurrence


def test_window_launch_is_counted_once_and_matching_is_current(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence = runtime_occurrence(runtime_payload)
    rule = configured_app_rule(config_payload)

    quota = AppQuota(occurrence, rule)
    first = quota.reconcile((42, 43))
    changed_title = quota.reconcile((43,))
    returned_title = quota.reconcile((42, 43))

    assert first.allowed
    assert first.usage is not None
    assert first.usage.wnds == [41, 42, 43]
    assert changed_title.usage is first.usage
    assert returned_title.usage is first.usage
    assert first.usage.active_wnds == [42, 43]
    assert len(first.usage.wnds) == 3


def test_launch_limit_blocks_only_windows_beyond_the_allowance(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence = runtime_occurrence(runtime_payload)
    rule = configured_app_rule(config_payload, max_launches=2)

    result = AppQuota(occurrence, rule).reconcile((42, 43))

    assert result.launch_blocked == (43,)
    assert result.time_blocked == ()
    assert result.blocked_wnds == (43,)


def test_time_limit_blocks_every_currently_matching_window(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence = runtime_occurrence(runtime_payload)
    rule = configured_app_rule(config_payload, max_time=918)

    result = AppQuota(occurrence, rule).reconcile((42, 43))

    assert result.time_blocked == (42, 43)
    assert result.blocked_wnds == (42, 43)


def test_multiple_windows_spend_time_only_once(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence = runtime_occurrence(runtime_payload)
    rule = configured_app_rule(config_payload)
    quota = AppQuota(occurrence, rule)
    usage = quota.reconcile((42, 43)).usage
    assert usage is not None

    result = quota.spend(10)

    assert usage.spent == 928
    assert result.allowed


def test_disabled_rule_clears_active_windows_without_spending(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    occurrence = runtime_occurrence(runtime_payload)
    rule = configured_app_rule(config_payload, apply=False)
    usage = occurrence.apps[0]

    quota = AppQuota(occurrence, rule)
    reconciled = quota.reconcile((42,))
    spent = quota.spend(100)

    assert reconciled.allowed
    assert usage.active_wnds == []
    assert usage.spent == 918
    assert spent.allowed
