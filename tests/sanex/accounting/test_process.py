"""Tests for reconciling AT-SPI windows with mutable runtime state."""

from typing import Any

import pytest
from sanelib.protocol import Actor, EventType, ProcessEndEvent, ProcessStartEvent

from sanex.accounting.runtime import AccountRuntime
from sanex.model.process import ProcessIdentity
from sanex.model.state import ProcessRun, WindowState, decode_runtime_state
from sanex.model.window import (
    AtspiWindow,
    AtspiWindowReference,
    AtspiWindowSnapshot,
    WindowRole,
)
from sanex.platform.process import ProcessWindowGroup


def existing_group(runtime_payload: dict[str, Any], extra_window: bool = False) -> ProcessWindowGroup:
    """Build a process group matching the active fixture run."""
    state = decode_runtime_state(runtime_payload)
    existing = state.wnds[0]
    process_run = state.prc_runs[0]
    process = ProcessIdentity(
        pid=existing.pid,
        parent_pid=1,
        uid=1001,
        started=existing.prc_started,
        prc_name=process_run.prc_name,
        exe=process_run.exe,
    )
    windows = [
        AtspiWindow(
            bus=existing.bus,
            path=existing.path,
            pid=existing.pid,
            title="Browser",
            role=WindowRole.FRAME,
        )
    ]

    if extra_window:
        windows.append(
            AtspiWindow(
                bus=existing.bus,
                path="/org/a11y/atspi/accessible/125",
                pid=existing.pid,
                title="Second window",
                role=WindowRole.FRAME,
            )
        )

    return ProcessWindowGroup(
        process=process,
        windows=tuple(windows),
        owners=(process,),
    )


def test_preserves_window_identity_and_extends_existing_run(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    existing_run = state.prc_runs[0]

    result = AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(existing_group(runtime_payload, extra_window=True),),
        snapshot=AtspiWindowSnapshot(),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert [window.state.ident for window in result.windows] == [42, 43]
    assert result.started == ()
    assert result.ended == ()
    assert state.wnd_seq == 43
    assert state.prc_runs == [existing_run]
    assert existing_run.duration == 3142
    assert existing_run.wnds == [42, 43]


def test_replaces_worker_window_without_starting_another_application_run(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.prc_runs[0]
    root = ProcessIdentity(
        pid=state.wnds[0].pid,
        parent_pid=1,
        uid=1001,
        started=state.wnds[0].prc_started,
        prc_name=run.prc_name,
        exe=run.exe,
    )
    run.group_ident = f"{root.pid}:{root.started}"
    worker = ProcessIdentity(
        pid=root.pid + 1,
        parent_pid=root.pid,
        uid=root.uid,
        started=root.started + 10,
        prc_name=root.prc_name,
        exe=root.exe,
    )
    observed = AtspiWindow(
        bus=state.wnds[0].bus,
        path="/org/a11y/atspi/accessible/worker",
        pid=worker.pid,
        title="Browser",
        role=WindowRole.FRAME,
    )
    group = ProcessWindowGroup(
        process=root,
        windows=(observed,),
        owners=(worker,),
    )

    result = AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(group,),
        snapshot=AtspiWindowSnapshot(),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert result.started == ()
    assert result.ended == ()
    assert state.prc_runs == [run]
    assert run.wnds == [43]
    assert state.wnds[0].pid == worker.pid
    assert result.windows[0].process == root


def test_preserves_run_when_its_available_process_root_changes(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.prc_runs[0]
    previous = existing_group(runtime_payload)
    root = previous.process.model_copy(update={"pid": 6400, "started": 5_753_000})
    owner = previous.process.model_copy(update={"parent_pid": root.pid})
    group = ProcessWindowGroup(process=root, windows=previous.windows, owners=(owner,))

    result = AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(group,),
        snapshot=AtspiWindowSnapshot(),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert result.started == ()
    assert result.ended == ()
    assert state.prc_runs == [run]
    assert run.group_ident == group.ident
    assert run.wnds == [42]
    assert run.duration == 3142


def test_missing_windows_end_run_after_charging_final_interval(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    existing_run = state.prc_runs[0]

    result = AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(),
        snapshot=AtspiWindowSnapshot(),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert result.windows == ()
    assert result.started == ()
    assert result.ended == (existing_run,)
    assert existing_run.duration == 3142
    assert state.wnds == []
    assert state.prc_runs == []


@pytest.mark.parametrize("scope", ["application", "window"])
def test_inaccessible_snapshot_part_preserves_its_existing_process_run(
    runtime_payload: dict[str, Any],
    scope: str,
) -> None:
    state = decode_runtime_state(runtime_payload)
    existing_window = state.wnds[0]
    existing_run = state.prc_runs[0]

    if scope == "application":
        snapshot = AtspiWindowSnapshot(unavailable_buses=(existing_window.bus,))

    else:
        snapshot = AtspiWindowSnapshot(
            unavailable_windows=(
                AtspiWindowReference(
                    bus=existing_window.bus,
                    path=existing_window.path,
                ),
            ),
        )

    result = AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(),
        snapshot=snapshot,
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert result.started == ()
    assert result.ended == ()
    assert state.wnds == [existing_window]
    assert state.prc_runs == [existing_run]
    assert existing_run.duration == 3142


def test_pid_reuse_creates_new_window_and_process_run(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    old_run = state.prc_runs[0]
    group = existing_group(runtime_payload)
    replacement_process = group.process.model_copy(update={"started": group.process.started + 1})
    replacement = ProcessWindowGroup(
        process=replacement_process,
        windows=group.windows,
        owners=(replacement_process,),
    )

    result = AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(replacement,),
        snapshot=AtspiWindowSnapshot(),
        timestamp=1_790_845_600,
        elapsed=1,
    )

    assert [window.state.ident for window in result.windows] == [43]
    assert result.ended == (old_run,)
    assert len(result.started) == 1
    assert result.started[0].wnds == [43]
    assert result.started[0].duration == 0


def test_other_session_windows_and_runs_are_preserved(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    other_window = WindowState(
        ident=43,
        sess_ident="4",
        bus=":1.200",
        path="/org/a11y/atspi/accessible/1",
        pid=7000,
        prc_started=6_000_000,
    )
    other_run = ProcessRun(
        run_ident=None,
        group_ident=f"{other_window.pid}:{other_window.prc_started}",
        prc_name="editor",
        exe="/usr/bin/editor",
        started=1_790_845_000,
        duration=10,
        reported=False,
        wnds=[43],
    )
    state.wnd_seq = 43
    state.wnds.append(other_window)
    state.prc_runs.append(other_run)

    AccountRuntime(1001, state).reconcile_windows(
        sess_ident="3",
        groups=(),
        snapshot=AtspiWindowSnapshot(),
        timestamp=1_790_845_600,
        elapsed=5,
    )

    assert state.wnds == [other_window]
    assert state.prc_runs == [other_run]
    assert other_run.duration == 10


def test_active_run_is_reported_at_exact_duration_threshold(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.prc_runs[0]
    run.run_ident = None
    run.reported = False
    run.duration = 5

    events = AccountRuntime(1001, state).create_process_events( ended=(), min_duration=5, timestamp=1_790_845_600)

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ProcessStartEvent)
    assert event.type is EventType.PRC_START
    assert event.seq == 142
    assert event.run_ident == 142
    assert event.timestamp == run.started
    assert run.reported
    assert run.run_ident == 142
    assert state.event_seq == 142


def test_short_completed_run_is_discarded(runtime_payload: dict[str, Any]) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.prc_runs.pop()
    run.run_ident = None
    run.reported = False
    run.duration = 4

    assert AccountRuntime(1001, state).create_process_events(
        ended=(run,),
        min_duration=5,
        timestamp=1_790_845_600,
    ) == ()
    assert state.event_seq == 141


def test_completed_run_crossing_threshold_emits_start_then_end(
    runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.prc_runs.pop()
    run.run_ident = None
    run.reported = False
    run.duration = 5

    events = AccountRuntime(1001, state).create_process_events(
        ended=(run,),
        min_duration=5,
        timestamp=1_790_845_600,
        actor=Actor.USER,
    )

    assert [event.type for event in events] == [EventType.PRC_START, EventType.PRC_END]
    start, end = events
    assert isinstance(start, ProcessStartEvent)
    assert isinstance(end, ProcessEndEvent)
    assert start.seq == 142
    assert end.seq == 143
    assert end.run_ident == start.run_ident
    assert end.duration == 5
    assert end.meta.actor is Actor.USER
    assert state.event_seq == 143


def test_reported_completed_run_emits_only_end(runtime_payload: dict[str, Any]) -> None:
    state = decode_runtime_state(runtime_payload)
    run = state.prc_runs.pop()

    events = AccountRuntime(1001, state).create_process_events(
        ended=(run,),
        min_duration=5,
        timestamp=1_790_845_600,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ProcessEndEvent)
    assert event.seq == 142
    assert event.run_ident == 121
    assert event.meta.actor is Actor.UNKNOWN
