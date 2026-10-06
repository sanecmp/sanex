"""Reconciliation and event reporting for graphical login sessions."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sanelib.protocol import Actor, EndMeta, Event, EventType, SessionEndEvent, SessionStartEvent, SessionStartMeta

from ..exceptions import AccountingError
from ..model.session import LoginSession
from ..model.state import SessionRun
from .validation import require_nonnegative

if TYPE_CHECKING:
    from .runtime import AccountRuntime


@dataclass(frozen=True, slots=True)
class SessionReconciliation:
    """Session runs and events changed by one complete logind snapshot."""

    started: tuple[SessionRun, ...]
    ended: tuple[SessionRun, ...]
    events: tuple[Event, ...]


def _reconcile_login_sessions(
    runtime: "AccountRuntime",
    sessions: Iterable[LoginSession],
    timestamp: int,
    elapsed: int,
    existing: bool = False,
) -> SessionReconciliation:
    """Reconcile graphical sessions for one account and emit lifecycle events."""
    state = runtime.state
    require_nonnegative(timestamp, "session timestamp")
    require_nonnegative(elapsed, "session elapsed time")

    if not isinstance(existing, bool):
        raise AccountingError("existing session flag is invalid")

    current = tuple(sorted(sessions, key=lambda session: session.ident))

    if len({session.ident for session in current}) != len(current):
        raise AccountingError("logind snapshot contains duplicate session ident")

    previous = {run.sess_ident: run for run in state.sess_runs}

    for run in previous.values():
        run.duration += elapsed

    current_runs: list[SessionRun] = []
    started_runs: list[SessionRun] = []
    events: list[Event] = []

    for session in current:
        run = previous.pop(session.ident, None)

        if run is None:
            event_seq = runtime.next_event_seq()
            run = SessionRun(
                run_ident=event_seq,
                sess_ident=session.ident,
                started=session.started,
                duration=0,
                terminate_requested=None,
                break_duration=0,
            )
            started_runs.append(run)
            events.append(
                SessionStartEvent(
                    seq=event_seq,
                    type=EventType.SESSION_START,
                    timestamp=session.started,
                    run_ident=event_seq,
                    sess_ident=session.ident,
                    meta=SessionStartMeta(existing=existing),
                )
            )

        current_runs.append(run)

    ended_runs = tuple(sorted(previous.values(), key=lambda run: run.sess_ident))

    for run in ended_runs:
        usages = runtime.active_session_usages(run.sess_ident)
        actor = (
            Actor.SANEX
            if run.terminate_requested is not None
            or any(usage.terminate_requested is not None for usage in usages)
            else Actor.UNKNOWN
        )

        for usage in usages:
            runtime.finish_session(usage, timestamp)

        if run.terminate_requested is not None and run.break_duration > 0:
            candidate = timestamp + run.break_duration
            state.break_till = max(state.break_till or 0, candidate)

        event_seq = runtime.next_event_seq()
        events.append(
            SessionEndEvent(
                seq=event_seq,
                type=EventType.SESSION_END,
                timestamp=timestamp,
                run_ident=run.run_ident,
                duration=run.duration,
                meta=EndMeta(actor=actor),
            )
        )

    state.sess_runs = current_runs
    return SessionReconciliation(
        started=tuple(started_runs),
        ended=ended_runs,
        events=tuple(events),
    )
