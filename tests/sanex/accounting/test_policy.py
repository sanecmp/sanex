"""Tests for unified account session-policy evaluation."""

from typing import Any

from sanelib.protocol import Account, Config, decode_config

from sanex.accounting.policy import SessionDenial, SessionPolicyEvaluator
from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.schedule import ScheduleResolver
from sanex.model.state import RuntimeState, decode_runtime_state


ACTIVE_TIMESTAMP = 1_790_956_800
OUTSIDE_TIMESTAMP = 1_790_964_000


def policy_context(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> tuple[Config, Account, RuntimeState, ScheduleResolver]:
    """Load config and align the fixture occurrence with the active test range."""
    runtime_payload["occurrences"][0]["date"] = "2026-10-02"
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    return config, config.accounts[0], state, ScheduleResolver.for_config(config)


def test_disabled_account_does_not_spend_or_enforce(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver = policy_context(config_payload, runtime_payload)
    account = account.model_copy(update={"apply": False})
    usage = state.occurrences[0].sessions[0]

    decision = SessionPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        sess_ident="3",
        timestamp=ACTIVE_TIMESTAMP,
        elapsed=10,
    )

    assert decision.allowed
    assert not decision.terminate
    assert usage.spent == 812
    assert state.sess_runs[0].terminate_requested is None


def test_mandatory_break_requests_termination_only_once(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver = policy_context(config_payload, runtime_payload)
    state.break_till = ACTIVE_TIMESTAMP + 100

    evaluator = SessionPolicyEvaluator(AccountRuntime(1001, state), resolver)
    first = evaluator.evaluate(
        account, "3", ACTIVE_TIMESTAMP, elapsed=1
    )
    repeated = evaluator.evaluate(
        account, "3", ACTIVE_TIMESTAMP + 1, elapsed=1
    )

    assert not first.allowed
    assert first.reason is SessionDenial.MANDATORY_BREAK
    assert first.terminate
    assert not repeated.terminate
    assert state.sess_runs[0].terminate_requested == ACTIVE_TIMESTAMP
    assert state.sess_runs[0].break_duration == 0


def test_outside_schedule_uses_latest_session_break_duration(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver = policy_context(config_payload, runtime_payload)

    decision = SessionPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        sess_ident="3",
        timestamp=OUTSIDE_TIMESTAMP,
        elapsed=1,
    )

    assert not decision.allowed
    assert decision.reason is SessionDenial.OUTSIDE_RANGE
    assert decision.terminate
    assert state.sess_runs[0].break_duration == 7200


def test_rejected_new_session_does_not_consume_session_count(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["max_sessions"] = 0
    config, account, state, resolver = policy_context(config_payload, runtime_payload)
    state.sess_runs[0].sess_ident = "4"
    occurrence = state.occurrences[0]

    decision = SessionPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        sess_ident="4",
        timestamp=ACTIVE_TIMESTAMP,
        elapsed=1,
    )

    assert not decision.allowed
    assert decision.reason is SessionDenial.MAX_SESSIONS
    assert decision.usage is None
    assert len(occurrence.sessions) == 1
    assert state.sess_runs[0].break_duration == 7200


def test_existing_session_spends_time_and_reaches_duration_limit(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["max_duration"] = 822
    config, account, state, resolver = policy_context(config_payload, runtime_payload)

    decision = SessionPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        sess_ident="3",
        timestamp=ACTIVE_TIMESTAMP,
        elapsed=10,
    )

    assert not decision.allowed
    assert decision.reason is SessionDenial.MAX_DURATION
    assert decision.usage is not None
    assert decision.usage.spent == 822
    assert state.sess_runs[0].terminate_requested == ACTIVE_TIMESTAMP


def test_forced_break_starts_when_session_actually_ends(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver = policy_context(config_payload, runtime_payload)
    SessionPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        sess_ident="3",
        timestamp=OUTSIDE_TIMESTAMP,
        elapsed=1,
    )

    AccountRuntime(1001, state).reconcile_sessions(
        sessions=(),
        timestamp=OUTSIDE_TIMESTAMP + 10,
        elapsed=1,
    )

    assert state.break_till == OUTSIDE_TIMESTAMP + 10 + 7200


def test_disabled_session_rule_does_not_spend_or_enforce(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["apply"] = False
    config, account, state, resolver = policy_context(config_payload, runtime_payload)
    usage = state.occurrences[0].sessions[0]

    decision = SessionPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        sess_ident="3",
        timestamp=ACTIVE_TIMESTAMP,
        elapsed=10,
    )

    assert decision.allowed
    assert not decision.terminate
    assert usage.spent == 812
