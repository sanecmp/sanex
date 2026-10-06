"""Tests for best-effort enforcement of application-window decisions."""

from typing import Any

import pytest
from sanelib.protocol import EnforcementFailureReason

from sanex.accounting.app_policy import AppPolicyDecision, BlockedWindow
from sanex.accounting.process import ResolvedWindow
from sanex.accounting.runtime import AccountRuntime
from sanex.enforcement.app import AppEnforcer
from sanex.exceptions import AtspiError
from sanex.model.process import ProcessIdentity
from sanex.model.state import RuntimeState, decode_runtime_state
from sanex.model.window import AtspiWindow, WindowRole


class FakeCloser:
    """Return or raise configured outcomes for concrete windows."""

    def __init__(self, outcomes: list[bool | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[AtspiWindow] = []

    async def close_window(
        self, uid: int, sess_ident: str, window: AtspiWindow
    ) -> bool:
        self.calls.append(window)
        outcome = self.outcomes.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


def blocked_window(
    state: RuntimeState,
    rule_idents: tuple[int, ...] = (701,),
) -> BlockedWindow:
    """Build a blocked resolved window from the shared runtime fixture."""
    window = state.wnds[0]
    observed = AtspiWindow(
        bus=window.bus,
        path=window.path,
        pid=window.pid,
        title="Synthetic browser",
        role=WindowRole.FRAME,
    )
    process = ProcessIdentity(
        pid=window.pid,
        parent_pid=1,
        uid=1001,
        started=window.prc_started,
        prc_name="synthetic-browser",
        exe="/usr/bin/synthetic-browser",
    )
    return BlockedWindow(
        window=ResolvedWindow(state=window, observed=observed, process=process),
        rule_idents=rule_idents,
    )


@pytest.mark.asyncio
async def test_accepted_close_is_sent_once_and_times_out_on_next_snapshot(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    initial_seq = state.event_seq
    blocked = blocked_window(state, (701, 702))
    decision = AppPolicyDecision(blocked=(blocked,))
    closer = FakeCloser([True])
    enforcer = AppEnforcer(AccountRuntime(1001, state), closer)

    first = await enforcer.enforce(decision, 1_790_845_000)
    second = await enforcer.enforce(decision, 1_790_845_005)
    repeated = await enforcer.enforce(decision, 1_790_845_010)

    assert first == ()
    assert len(closer.calls) == 1
    assert [event.seq for event in second] == [initial_seq + 1, initial_seq + 2]
    assert [event.rule_ident for event in second] == [701, 702]
    assert all(
        event.reason is EnforcementFailureReason.CLOSE_TIMEOUT
        for event in second
    )
    assert repeated == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (False, EnforcementFailureReason.CLOSE_UNSUPPORTED),
        (
            AtspiError("close window", RuntimeError("rejected")),
            EnforcementFailureReason.CLOSE_FAILED,
        ),
    ],
)
async def test_immediate_close_failure_is_reported_once(
    runtime_payload: dict[str, Any],
    outcome: bool | Exception,
    reason: EnforcementFailureReason,
) -> None:
    state = decode_runtime_state(runtime_payload)
    decision = AppPolicyDecision(blocked=(blocked_window(state),))
    closer = FakeCloser([outcome])
    enforcer = AppEnforcer(AccountRuntime(1001, state), closer)

    events = await enforcer.enforce(decision, 1_790_845_000)
    repeated = await enforcer.enforce(decision, 1_790_845_005)

    assert len(closer.calls) == 1
    assert len(events) == 1
    assert events[0].reason is reason
    assert events[0].wnd_ident == state.wnds[0].ident
    assert events[0].rule_ident == 701
    assert repeated == ()


@pytest.mark.asyncio
async def test_disappearance_clears_attempt_and_allows_a_later_retry(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    decision = AppPolicyDecision(blocked=(blocked_window(state),))
    closer = FakeCloser([True, True])
    enforcer = AppEnforcer(AccountRuntime(1001, state), closer)

    await enforcer.enforce(decision, 1_790_845_000)
    await enforcer.enforce(AppPolicyDecision(), 1_790_845_005)
    events = await enforcer.enforce(decision, 1_790_845_010)

    assert len(closer.calls) == 2
    assert events == ()
