"""Tests for graphical login-session reconciliation and events."""

from pathlib import Path
from typing import Any

import pytest
from sanelib.protocol import Actor, EventType, SessionEndEvent, SessionStartEvent, UsageInterval

from sanex.accounting.runtime import AccountRuntime
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.state import decode_runtime_state
from sanex.storage.state import RuntimeStateStore


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


def test_session_intervals_exclude_time_before_discovery_sleep_and_service_restart(
    runtime_payload: dict[str, Any], tmp_path: Path,
) -> None:
    state = decode_runtime_state(runtime_payload)
    state.sess_runs = []
    runtime = AccountRuntime(1001, state)
    session = observed_session(runtime_payload)
    timestamp = session.started + 86_400

    started = runtime.reconcile_sessions((session,), timestamp, 0, existing=True)
    runtime.reconcile_sessions((session,), timestamp + 5, 5)
    runtime.reconcile_sessions((session,), timestamp + 10, 5)
    runtime.reconcile_sessions((session,), timestamp + 3_610, 0)
    runtime.reconcile_sessions((session,), timestamp + 3_615, 5)
    store = RuntimeStateStore(tmp_path)
    store.save(runtime.uid, state)
    restored = store.load(runtime.uid)
    assert restored is not None
    runtime = AccountRuntime(runtime.uid, restored)
    runtime.reconcile_sessions((session,), timestamp + 7_215, 0, existing=True)
    ended = runtime.reconcile_sessions((), timestamp + 7_220, 5)

    event = ended.events[0]
    assert started.events[0].timestamp == session.started
    assert event.duration == 20
    assert event.intervals == (
        UsageInterval(timestamp=timestamp + 10, duration=10),
        UsageInterval(timestamp=timestamp + 3_615, duration=5),
        UsageInterval(timestamp=timestamp + 7_220, duration=5),
    )
    assert sum(interval.duration for interval in event.intervals) == event.duration


def test_session_without_an_interval_history_keeps_it_unknown(runtime_payload: dict[str, Any]) -> None:
    state = decode_runtime_state(runtime_payload)
    runtime = AccountRuntime(1001, state)

    ended = runtime.reconcile_sessions((), 1_790_845_600, 5)

    assert ended.events[0].duration == 3_022
    assert ended.events[0].intervals is None
