"""Tests for the complete per-account processing cycle."""

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sanelib.protocol import Config, Event, EventType, decode_config

from sanex.accounting.cycle import AccountCycle, SessionObservation
from sanex.accounting.runtime import AccountRuntime
from sanex.exceptions import AccountingError
from sanex.model.process import ProcessIdentity
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.state import RuntimeState, decode_runtime_state
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.process import WindowProcessResolver


ACTIVE_TIMESTAMP = 1_790_956_800


class FakeProcessReader:
    """Return one stable identity for the fixture browser process."""

    def __init__(self, process: ProcessIdentity) -> None:
        self.process = process

    def read(self, pid: int) -> ProcessIdentity | None:
        return self.process if pid == self.process.pid else None


class FakeTerminator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def terminate(self, ident: str) -> None:
        self.calls.append(ident)


class FakeWindowCloser:
    def __init__(self) -> None:
        self.calls: list[AtspiWindow] = []

    async def close_window(
        self, uid: int, sess_ident: str, window: AtspiWindow
    ) -> bool:
        self.calls.append(window)
        return True


class FakeEventAppender:
    def __init__(self) -> None:
        self.events: list[tuple[int, Event]] = []

    def append(self, uid: int, event: Event) -> None:
        self.events.append((uid, event))


class FakeStateSaver:
    def __init__(self) -> None:
        self.states: list[tuple[int, RuntimeState]] = []

    def save(self, uid: int, state: RuntimeState) -> RuntimeState:
        snapshot = state.model_copy(deep=True)
        self.states.append((uid, snapshot))
        return snapshot


def account_observation(state: RuntimeState) -> tuple[SessionObservation, ProcessIdentity]:
    """Build a complete synthetic snapshot matching the persisted fixture state."""
    run = state.sess_runs[0]
    window = state.wnds[0]
    process_run = state.prc_runs[0]
    session = LoginSession(
        ident=run.sess_ident,
        uid=1001,
        login="child",
        path=f"/org/freedesktop/login1/session/_{run.sess_ident}",
        started=run.started,
        type=SessionType.WAYLAND,
        state=SessionState.ACTIVE,
        active=True,
        idle=False,
        locked=False,
    )
    observed_window = AtspiWindow(
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
        prc_name=process_run.prc_name,
        exe=process_run.exe,
    )
    return SessionObservation(
        session,
        AtspiWindowSnapshot(windows=(observed_window,)),
    ), process


def build_cycle(
    config: Config,
    state: RuntimeState,
    process: ProcessIdentity,
) -> tuple[AccountCycle, FakeTerminator, FakeWindowCloser, FakeEventAppender, FakeStateSaver]:
    """Build an account cycle with observable external dependencies."""
    terminator = FakeTerminator()
    closer = FakeWindowCloser()
    events = FakeEventAppender()
    saver = FakeStateSaver()
    cycle = AccountCycle(
        runtime=AccountRuntime(1001, state),
        config=config,
        process_resolver=WindowProcessResolver(FakeProcessReader(process)),
        terminator=terminator,
        window_closer=closer,
        event_appender=events,
        state_saver=saver,
    )
    return cycle, terminator, closer, events, saver


@pytest.mark.asyncio
async def test_complete_snapshot_updates_all_counters_and_saves_periodically(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    state.occurrences[0].date = date(2026, 10, 2)
    observation, process = account_observation(state)
    cycle, terminator, closer, events, saver = build_cycle(config, state, process)

    first = await cycle.run((observation,), ACTIVE_TIMESTAMP, elapsed=1)
    second = await cycle.run((observation,), ACTIVE_TIMESTAMP + 1, elapsed=1)
    third = await cycle.run((observation,), ACTIVE_TIMESTAMP + 5, elapsed=4)

    assert first.saved
    assert not second.saved
    assert third.saved
    assert len(saver.states) == 2
    assert state.sess_runs[0].duration == 3023
    assert state.prc_runs[0].duration == 3143
    assert state.occurrences[0].sessions[0].spent == 818
    assert state.occurrences[0].apps[0].spent == 924
    assert events.events == []
    assert terminator.calls == []
    assert closer.calls == []


@pytest.mark.asyncio
async def test_disabled_enforcement_keeps_accounting_without_external_actions(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    observation, process = account_observation(state)
    cycle, terminator, closer, events, saver = build_cycle(config, state, process)
    cycle.enforcement_enabled = False
    session_enforce = AsyncMock(return_value=True)
    app_enforce = AsyncMock(return_value=())
    cycle._session_enforcer = SimpleNamespace(enforce=session_enforce)
    cycle._app_enforcer = SimpleNamespace(enforce=app_enforce)

    result = await cycle.run((observation,), ACTIVE_TIMESTAMP, elapsed=1)

    assert result.session_decisions
    session_enforce.assert_not_awaited()
    app_enforce.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_session_finishes_session_and_process_in_sequence_order(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    observation, process = account_observation(state)
    cycle, terminator, closer, event_appender, saver = build_cycle(config, state, process)

    result = await cycle.run((), ACTIVE_TIMESTAMP, elapsed=5)

    assert [event.type for event in result.events] == [
        EventType.SESSION_END,
        EventType.PRC_END,
    ]
    assert [event.seq for event in result.events] == [142, 143]
    assert [uid for uid, event in event_appender.events] == [1001, 1001]
    assert state.sess_runs == []
    assert state.prc_runs == []
    assert state.wnds == []


@pytest.mark.asyncio
async def test_disabled_collection_clears_live_state_without_events(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["accounts"][0].update({"collect": False, "apply": False})
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    observation, process = account_observation(state)
    state.occurrences[0].sessions[0].terminate_requested = ACTIVE_TIMESTAMP
    cycle, terminator, closer, events, saver = build_cycle(config, state, process)

    result = await cycle.run((observation,), ACTIVE_TIMESTAMP, elapsed=1)

    assert result.events == ()
    assert result.session_decisions == ()
    assert result.app_decision.blocked == ()
    assert result.saved
    assert state.sess_runs == []
    assert state.prc_runs == []
    assert state.wnds == []
    assert not state.occurrences[0].sessions[0].active
    assert state.occurrences[0].sessions[0].terminate_requested is None
    assert state.occurrences[0].apps[0].active_wnds == []
    assert terminator.calls == []
    assert closer.calls == []
    assert events.events == []
    assert len(saver.states) == 1


@pytest.mark.asyncio
async def test_foreign_session_is_rejected_before_state_changes(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    observation, process = account_observation(state)
    foreign = SessionObservation(
        observation.session.model_copy(update={"uid": 1002}),
        observation.snapshot,
    )
    cycle, terminator, closer, events, saver = build_cycle(config, state, process)

    with pytest.raises(AccountingError, match="belongs to UID 1002"):
        await cycle.run((foreign,), ACTIVE_TIMESTAMP, elapsed=1)

    assert state.sess_runs[0].duration == 3017
    assert saver.states == []


@pytest.mark.asyncio
async def test_unavailable_window_snapshot_preserves_objects_and_advances_known_usage(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    state.occurrences[0].date = date(2026, 10, 2)
    observation, process = account_observation(state)
    unavailable = SessionObservation(observation.session, None)
    cycle, terminator, closer, events, saver = build_cycle(config, state, process)

    result = await cycle.run((unavailable,), ACTIVE_TIMESTAMP, elapsed=5)

    assert result.app_decision.blocked == ()
    assert state.prc_runs[0].duration == 3142
    assert state.wnds[0].ident == 42
    assert state.occurrences[0].apps[0].spent == 923
    assert state.occurrences[0].apps[0].active_wnds == [42]
    assert closer.calls == []
    assert events.events == []


@pytest.mark.asyncio
async def test_recovered_window_snapshot_continues_existing_process_run(
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    state.occurrences[0].date = date(2026, 10, 2)
    observation, process = account_observation(state)
    unavailable = SessionObservation(observation.session, None)
    process_run = state.prc_runs[0]
    cycle, terminator, closer, events, saver = build_cycle(config, state, process)

    await cycle.run((unavailable,), ACTIVE_TIMESTAMP, elapsed=5)
    result = await cycle.run((observation,), ACTIVE_TIMESTAMP + 5, elapsed=1)

    assert result.events == ()
    assert state.prc_runs == [process_run]
    assert state.prc_runs[0] is process_run
    assert process_run.duration == 3143
    assert process_run.wnds == [42]
    assert state.wnds[0].ident == 42
    assert state.event_seq == 141
    assert closer.calls == []
    assert events.events == []
