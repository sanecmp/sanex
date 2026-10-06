"""Tests for execution and persistence of session termination decisions."""

from typing import Any

import pytest

from sanex.accounting.policy import SessionDecision, SessionDenial
from sanex.accounting.runtime import AccountRuntime
from sanex.enforcement.session import SessionEnforcer
from sanex.exceptions import LogindError
from sanex.model.state import RuntimeState, decode_runtime_state


class FakeTerminator:
    """Record exact session termination calls and optionally fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def terminate(self, ident: str) -> None:
        self.calls.append(ident)

        if self.error is not None:
            raise self.error


class FakeStateSaver:
    """Record immutable snapshots passed to persistence."""

    def __init__(self) -> None:
        self.saved: list[tuple[int, RuntimeState]] = []

    def save(self, uid: int, state: RuntimeState) -> RuntimeState:
        snapshot = state.model_copy(deep=True)
        self.saved.append((uid, snapshot))
        return snapshot


def denied_decision() -> SessionDecision:
    """Build a newly actionable schedule denial."""
    return SessionDecision(
        allowed=False,
        reason=SessionDenial.OUTSIDE_RANGE,
        terminate=True,
    )


@pytest.mark.asyncio
async def test_success_terminates_exact_session_and_persists_request(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.sess_runs[0]
    run.terminate_requested = 1_790_845_000
    run.break_duration = 7200
    terminator = FakeTerminator()
    saver = FakeStateSaver()

    sent = await SessionEnforcer(AccountRuntime(1001, state), terminator, saver).enforce(
        sess_ident="3",
        decision=denied_decision(),
    )

    assert sent
    assert terminator.calls == ["3"]
    assert len(saver.saved) == 1
    uid, snapshot = saver.saved[0]
    assert uid == 1001
    assert snapshot.sess_runs[0].terminate_requested == 1_790_845_000
    assert snapshot.sess_runs[0].break_duration == 7200


@pytest.mark.asyncio
async def test_failed_termination_rolls_back_and_persists_retryable_state(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.sess_runs[0]
    run.terminate_requested = 1_790_845_000
    run.break_duration = 7200
    terminator = FakeTerminator(LogindError("terminate session 3", RuntimeError("down")))
    saver = FakeStateSaver()

    with pytest.raises(LogindError, match="down"):
        await SessionEnforcer(AccountRuntime(1001, state), terminator, saver).enforce(
            sess_ident="3",
            decision=denied_decision(),
        )

    assert terminator.calls == ["3"]
    assert run.terminate_requested is None
    assert run.break_duration == 0
    assert len(saver.saved) == 2
    assert saver.saved[0][1].sess_runs[0].terminate_requested == 1_790_845_000
    assert saver.saved[1][1].sess_runs[0].terminate_requested is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        SessionDecision(allowed=True),
        SessionDecision(
            allowed=False,
            reason=SessionDenial.OUTSIDE_RANGE,
            terminate=False,
        ),
    ],
)
async def test_non_actionable_decision_does_nothing(
    runtime_payload: dict[str, Any],
    decision: SessionDecision,
) -> None:
    state = decode_runtime_state(runtime_payload)
    terminator = FakeTerminator()
    saver = FakeStateSaver()

    assert not await SessionEnforcer(AccountRuntime(1001, state), terminator, saver).enforce(
        sess_ident="3",
        decision=decision,
    )
    assert terminator.calls == []
    assert saver.saved == []
