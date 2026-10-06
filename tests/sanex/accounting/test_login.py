"""Tests for graphical login-session reconciliation and events."""

from typing import Any

import pytest
from sanelib.protocol import Actor, EventType, SessionEndEvent, SessionStartEvent

from sanex.accounting.runtime import AccountRuntime
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.state import decode_runtime_state


def observed_session(runtime_payload: dict[str, Any], ident: str = "3") -> LoginSession:
    """Build a graphical session aligned with the runtime fixture."""
    started = runtime_payload["sess_runs"][0]["started"]
    return LoginSession(
        ident=ident,
        uid=1001,
        login="child",
        path=f"/org/freedesktop/login1/session/_{ident}",
        started=started,
        type=SessionType.WAYLAND,
        state=SessionState.ACTIVE,
        active=True,
        idle=False,
        locked=False,
    )


def test_existing_run_accumulates_without_duplicate_event(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.sess_runs[0]

    result = AccountRuntime(1001, state).reconcile_sessions(
        sessions=(observed_session(runtime_payload),),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert result.started == ()
    assert result.ended == ()
    assert result.events == ()
    assert run.duration == 3022
    assert state.sess_runs == [run]


@pytest.mark.parametrize("existing", [False, True])
def test_new_session_emits_start_with_discovery_status(
    runtime_payload: dict[str, Any],
    existing: bool,
) -> None:
    state = decode_runtime_state(runtime_payload)
    state.sess_runs = []
    session = observed_session(runtime_payload, ident="4")

    result = AccountRuntime(1001, state).reconcile_sessions(
        sessions=(session,),
        timestamp=1_790_845_600,
        elapsed=5,
        existing=existing,
    )

    assert len(result.started) == 1
    assert result.ended == ()
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, SessionStartEvent)
    assert event.type is EventType.SESSION_START
    assert event.seq == 142
    assert event.run_ident == 142
    assert event.sess_ident == "4"
    assert event.timestamp == session.started
    assert event.meta.existing is existing
    assert state.event_seq == 142


def test_missing_session_emits_end_after_final_interval(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.sess_runs[0]

    result = AccountRuntime(1001, state).reconcile_sessions(
        sessions=(),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert result.started == ()
    assert result.ended == (run,)
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, SessionEndEvent)
    assert event.type is EventType.SESSION_END
    assert event.seq == 142
    assert event.run_ident == run.run_ident
    assert event.duration == 3022
    assert event.meta.actor is Actor.UNKNOWN
    assert state.sess_runs == []


def test_enforced_end_finishes_usage_and_starts_break(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    usage = state.occurrences[0].sessions[0]
    usage.terminate_requested = 1_790_845_500

    result = AccountRuntime(1001, state).reconcile_sessions(
        sessions=(),
        timestamp=1_790_845_600,
        elapsed=1,
    )

    event = result.events[0]
    assert isinstance(event, SessionEndEvent)
    assert event.meta.actor is Actor.SANEX
    assert not usage.active
    assert state.break_till == 1_790_852_800
