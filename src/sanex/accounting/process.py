"""Reconciliation of per-session window snapshots with mutable runtime state."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sanelib.protocol import (
    Actor,
    EndMeta,
    Event,
    EventType,
    ObservationSource,
    ProcessEndEvent,
    ProcessStartEvent,
    ProcessStartMeta,
)

from ..exceptions import AccountingError
from ..model.process import ProcessIdentity
from ..model.state import ProcessRun, WindowState
from ..model.window import AtspiWindow, AtspiWindowSnapshot
from ..platform.process import ProcessWindowGroup
from .validation import require_nonnegative

if TYPE_CHECKING:
    from .runtime import AccountRuntime


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    """An observed window associated with its persistent local identity."""

    state: WindowState
    observed: AtspiWindow
    process: ProcessIdentity


@dataclass(frozen=True, slots=True)
class ProcessReconciliation:
    """Current windows and application runs started or ended by one snapshot."""

    windows: tuple[ResolvedWindow, ...]
    unavailable_wnds: tuple[int, ...]
    started: tuple[ProcessRun, ...]
    ended: tuple[ProcessRun, ...]


def _reconcile_session_windows(
    runtime: "AccountRuntime",
    sess_ident: str,
    groups: Iterable[ProcessWindowGroup],
    snapshot: AtspiWindowSnapshot,
    timestamp: int,
    elapsed: int,
) -> ProcessReconciliation:
    """Reconcile one complete user-session AT-SPI snapshot."""
    state = runtime.state

    if not sess_ident:
        raise AccountingError("window session ident is empty")

    require_nonnegative(timestamp, "window timestamp")
    require_nonnegative(elapsed, "window elapsed time")

    previous_windows = {
        window.ident: window
        for window in state.wnds
        if window.sess_ident == sess_ident
    }
    other_windows = [window for window in state.wnds if window.sess_ident != sess_ident]
    previous_runs, other_runs = _split_session_runs(state.prc_runs, previous_windows)
    previous_run_windows = {id(run): tuple(run.wnds) for run in previous_runs}

    for run in previous_runs:
        run.duration += elapsed

    unavailable_buses = frozenset(snapshot.unavailable_buses)
    unavailable_windows = {
        (window.bus, window.path)
        for window in snapshot.unavailable_windows
    }
    preserved_windows = {
        window.ident: window
        for window in previous_windows.values()
        if window.bus in unavailable_buses
        or (window.bus, window.path) in unavailable_windows
    }

    known_windows = {
        (window.bus, window.path, window.pid, window.prc_started): window
        for window in previous_windows.values()
    }
    runs_by_window = {
        wnd_ident: run
        for run in previous_runs
        for wnd_ident in run.wnds
    }
    runs_by_group = {run.group_ident: run for run in previous_runs}

    if len(runs_by_group) != len(previous_runs):
        raise AccountingError("active process group identity is not unique")

    current_windows: list[WindowState] = []
    resolved_windows: list[ResolvedWindow] = []
    current_runs: list[ProcessRun] = []
    started_runs: list[ProcessRun] = []

    for group in groups:
        group_windows: list[WindowState] = []
        candidate_runs: dict[int, ProcessRun] = {}
        grouped_run = runs_by_group.get(group.ident)

        if grouped_run is not None:
            candidate_runs[id(grouped_run)] = grouped_run

        for observed in group.windows:
            owner = group.find_owner(observed)
            key = (observed.bus, observed.path, owner.pid, owner.started)
            window = known_windows.get(key)

            if window is None:
                state.wnd_seq += 1
                window = WindowState(
                    ident=state.wnd_seq,
                    sess_ident=sess_ident,
                    bus=observed.bus,
                    path=observed.path,
                    pid=owner.pid,
                    prc_started=owner.started,
                )

            group_windows.append(window)
            current_windows.append(window)
            resolved_windows.append(
                ResolvedWindow(state=window, observed=observed, process=group.process)
            )
            run = runs_by_window.get(window.ident)

            if run is not None:
                candidate_runs[id(run)] = run

        if len(candidate_runs) > 1:
            raise AccountingError("one process group references multiple active runs")

        run = next(iter(candidate_runs.values()), None)

        if run is None:
            run = ProcessRun(
                run_ident=None,
                group_ident=group.ident,
                prc_name=group.process.prc_name,
                exe=group.process.exe,
                started=timestamp,
                duration=0,
                reported=False,
                wnds=[],
            )
            started_runs.append(run)

        elif run.prc_name != group.process.prc_name or run.exe != group.process.exe:
            raise AccountingError("stable process identity changed its name or executable")

        run.group_ident = group.ident
        run.wnds = sorted(window.ident for window in group_windows)
        current_runs.append(run)

    current_run_ids = {id(run) for run in current_runs}

    for run in previous_runs:
        preserved_idents = [
            wnd_ident
            for wnd_ident in previous_run_windows[id(run)]
            if wnd_ident in preserved_windows
        ]

        if not preserved_idents:
            continue

        if id(run) not in current_run_ids:
            current_runs.append(run)
            current_run_ids.add(id(run))

        run.wnds = sorted({*run.wnds, *preserved_idents})

    current_windows.extend(preserved_windows.values())

    ended_runs = tuple(run for run in previous_runs if id(run) not in current_run_ids)
    state.wnds = sorted((*other_windows, *current_windows), key=lambda window: window.ident)
    state.prc_runs = [*other_runs, *current_runs]
    return ProcessReconciliation(
        windows=tuple(sorted(resolved_windows, key=lambda window: window.state.ident)),
        unavailable_wnds=tuple(sorted(preserved_windows)),
        started=tuple(started_runs),
        ended=ended_runs,
    )


def _split_session_runs(
    runs: list[ProcessRun],
    windows: dict[int, WindowState],
) -> tuple[list[ProcessRun], list[ProcessRun]]:
    session_runs: list[ProcessRun] = []
    other_runs: list[ProcessRun] = []

    for run in runs:
        owned = [wnd_ident in windows for wnd_ident in run.wnds]

        if any(owned) and not all(owned):
            raise AccountingError("process run contains windows from multiple sessions")

        (session_runs if any(owned) else other_runs).append(run)

    return session_runs, other_runs


def _create_process_events(
    runtime: "AccountRuntime",
    ended: Iterable[ProcessRun],
    min_duration: int,
    timestamp: int,
    actor: Actor = Actor.UNKNOWN,
) -> tuple[Event, ...]:
    """Report threshold crossings and completed application runs."""
    state = runtime.state
    require_nonnegative(min_duration, "minimum process duration")
    require_nonnegative(timestamp, "process event timestamp")

    if not isinstance(actor, Actor):
        raise AccountingError("process end actor is invalid")

    ended_runs = tuple(ended)
    ended_ids = {id(run) for run in ended_runs}
    candidates = sorted(
        (*state.prc_runs, *ended_runs),
        key=lambda run: (run.started, run.prc_name, run.exe, min(run.wnds, default=-1)),
    )
    events: list[Event] = []
    observed: set[int] = set()

    for run in candidates:
        identity = id(run)

        if identity in observed:
            continue

        observed.add(identity)

        if not run.reported and run.duration >= min_duration:
            event_seq = runtime.next_event_seq()
            run.run_ident = event_seq
            run.reported = True
            events.append(
                ProcessStartEvent(
                    seq=event_seq,
                    type=EventType.PRC_START,
                    timestamp=run.started,
                    run_ident=event_seq,
                    prc_name=run.prc_name,
                    exe=run.exe,
                    meta=ProcessStartMeta(observed_via=ObservationSource.ATSPI),
                )
            )

        if identity in ended_ids and run.reported:

            if run.run_ident is None:
                raise AccountingError("reported process run has no event identity")

            event_seq = runtime.next_event_seq()
            events.append(
                ProcessEndEvent(
                    seq=event_seq,
                    type=EventType.PRC_END,
                    timestamp=timestamp,
                    run_ident=run.run_ident,
                    duration=run.duration,
                    meta=EndMeta(actor=actor),
                )
            )

    return tuple(events)
