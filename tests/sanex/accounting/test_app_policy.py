"""Tests for unified application quota evaluation."""

from typing import Any

from sanelib.protocol import Account, Config, decode_config

from sanex.accounting.app_policy import AppPolicyEvaluator
from sanex.accounting.process import ResolvedWindow
from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.schedule import ScheduleResolver
from sanex.model.process import ProcessIdentity
from sanex.model.state import RuntimeState, decode_runtime_state
from sanex.model.window import AtspiWindow, WindowRole


ACTIVE_TIMESTAMP = 1_790_956_800
OUTSIDE_TIMESTAMP = 1_790_964_000


def policy_context(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> tuple[Config, Account, RuntimeState, ScheduleResolver, tuple[ResolvedWindow, ...]]:
    """Load aligned config, runtime state and one current browser window."""
    runtime_payload["occurrences"][0]["date"] = "2026-10-02"
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    window_state = state.wnds[0]
    process_run = state.prc_runs[0]
    process = ProcessIdentity(
        pid=window_state.pid,
        parent_pid=1,
        uid=account_uid(config),
        started=window_state.prc_started,
        prc_name=process_run.prc_name,
        exe=process_run.exe,
    )
    observed = AtspiWindow(
        bus=window_state.bus,
        path=window_state.path,
        pid=window_state.pid,
        title="Browser",
        role=WindowRole.FRAME,
    )
    windows = (ResolvedWindow(state=window_state, observed=observed, process=process),)
    return config, config.accounts[0], state, ScheduleResolver.for_config(config), windows


def account_uid(config: Config) -> int:
    """Return the UID of the shared single-account fixture."""
    return config.accounts[0].uid


def test_matching_rule_spends_one_interval(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)

    decision = AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account, windows, (), ACTIVE_TIMESTAMP, elapsed=10
    )

    usage = state.occurrences[0].apps[0]
    assert usage.spent == 928
    assert usage.active_wnds == [42]
    assert decision.blocked == ()


def test_time_limit_blocks_current_matching_window(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["app_rules"][0][
        "max_time"
    ] = 928
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)

    decision = AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account, windows, (), ACTIVE_TIMESTAMP, elapsed=10
    )

    assert len(decision.blocked) == 1
    assert decision.blocked[0].window is windows[0]
    assert decision.blocked[0].rule_idents == (701,)


def test_unavailable_match_is_preserved_while_available_window_is_enforced(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["app_rules"][0][
        "max_time"
    ] = 928
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)
    existing = windows[0]
    available = ResolvedWindow(
        state=existing.state.model_copy(
            update={
                "ident": 43,
                "path": "/org/a11y/atspi/accessible/43",
            }
        ),
        observed=existing.observed.model_copy(
            update={"path": "/org/a11y/atspi/accessible/43"}
        ),
        process=existing.process,
    )

    decision = AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account,
        (available,),
        (existing.state.ident,),
        ACTIVE_TIMESTAMP,
        elapsed=10,
    )

    usage = state.occurrences[0].apps[0]
    assert usage.spent == 928
    assert usage.active_wnds == [42, 43]
    assert len(decision.blocked) == 1
    assert decision.blocked[0].window is available


def test_multiple_violated_rules_close_window_only_once(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    app_rules = config_payload["accounts"][0]["limits"]["session_rules"][0]["app_rules"]
    app_rules[0]["max_time"] = 928
    second = dict(app_rules[0])
    second.update({"ident": 702, "name": "Second", "max_time": 0})
    app_rules.append(second)
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)

    decision = AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account, windows, (), ACTIVE_TIMESTAMP, elapsed=10
    )

    assert len(decision.blocked) == 1
    assert decision.blocked[0].rule_idents == (701, 702)


def test_disabled_account_clears_matches_without_spending(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)
    account = account.model_copy(update={"apply": False})

    decision = AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account, windows, (), ACTIVE_TIMESTAMP, elapsed=10
    )

    usage = state.occurrences[0].apps[0]
    assert usage.spent == 918
    assert usage.active_wnds == []
    assert decision.blocked == ()


def test_window_that_stops_matching_remains_counted_but_inactive(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0]["limits"]["session_rules"][0]["app_rules"][0].update(
        {
            "wnd_title": "School portal",
            "wnd_title_match": "exact",
        }
    )
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)

    AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account, windows, (), ACTIVE_TIMESTAMP, elapsed=10
    )

    usage = state.occurrences[0].apps[0]
    assert usage.spent == 928
    assert usage.wnds == [41, 42]
    assert usage.active_wnds == []


def test_outside_schedule_clears_active_windows(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config, account, state, resolver, windows = policy_context(config_payload, runtime_payload)

    decision = AppPolicyEvaluator(AccountRuntime(1001, state), resolver).evaluate(
        account, windows, (), OUTSIDE_TIMESTAMP, elapsed=10
    )

    usage = state.occurrences[0].apps[0]
    assert usage.spent == 918
    assert usage.active_wnds == []
    assert decision.blocked == ()
