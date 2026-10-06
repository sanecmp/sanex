"""Mutable accounting aggregate for one local operating-system account."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sanelib.protocol import Actor, Event

from ..model.session import LoginSession
from ..model.state import (
    ProcessRun,
    RangeOccurrence,
    RuntimeState,
    SessionRun,
    SessionUsage,
)
from ..model.window import AtspiWindowSnapshot
from ..platform.process import ProcessWindowGroup
from .login import SessionReconciliation, _reconcile_login_sessions
from .process import (
    ProcessReconciliation,
    _create_process_events,
    _reconcile_session_windows,
)
from .schedule import ActiveRange
from .validation import require_nonnegative


@dataclass(slots=True)
class AccountRuntime:
    """Own all mutable accounting state and sequence allocation for one UID."""

    uid: int
    state: RuntimeState

    def __post_init__(self) -> None:
        require_nonnegative(self.uid, "account runtime UID")

    def next_event_seq(self) -> int:
        """Allocate the next event identity for this account."""
        self.state.event_seq += 1
        return self.state.event_seq

    def find_occurrence(
        self,
        range_ident: int,
        occurrence_date: date,
    ) -> RangeOccurrence | None:
        """Find existing quota consumption for one range occurrence."""
        return next(
            (
                occurrence
                for occurrence in self.state.occurrences
                if occurrence.range_ident == range_ident
                and occurrence.date == occurrence_date
            ),
            None,
        )

    def ensure_occurrence(self, selected: ActiveRange) -> RangeOccurrence:
        """Return existing quota consumption or append an empty occurrence."""
        self.discard_completed_occurrences(selected.occurrence_date)
        occurrence = self.find_occurrence(
            selected.range.ident,
            selected.occurrence_date,
        )

        if occurrence is not None:
            return occurrence

        occurrence = RangeOccurrence(
            range_ident=selected.range.ident,
            date=selected.occurrence_date,
            sessions=[],
            apps=[],
        )
        self.state.occurrences.append(occurrence)
        return occurrence

    def discard_completed_occurrences(self, before: date) -> None:
        """Discard obsolete inactive quota consumption from earlier dates."""
        occurrences = self.state.occurrences
        self.state.occurrences = [
            occurrence
            for occurrence in occurrences
            if occurrence.date >= before
            or any(usage.active for usage in occurrence.sessions)
            or any(usage.active_wnds for usage in occurrence.apps)
        ]

    def reconcile_sessions(
        self,
        sessions: Iterable[LoginSession],
        timestamp: int,
        elapsed: int,
        existing: bool = False,
    ) -> SessionReconciliation:
        """Reconcile a complete graphical-session snapshot."""
        return _reconcile_login_sessions(
            self,
            sessions,
            timestamp,
            elapsed,
            existing,
        )

    def reconcile_windows(
        self,
        sess_ident: str,
        groups: Iterable[ProcessWindowGroup],
        snapshot: AtspiWindowSnapshot,
        timestamp: int,
        elapsed: int,
    ) -> ProcessReconciliation:
        """Reconcile a complete window snapshot for one login session."""
        return _reconcile_session_windows(
            self,
            sess_ident,
            groups,
            snapshot,
            timestamp,
            elapsed,
        )

    def advance_unobserved_processes(
        self,
        sess_ident: str,
        elapsed: int,
    ) -> None:
        """Advance known runs while preserving an unavailable window snapshot."""
        require_nonnegative(elapsed, "unobserved process elapsed time")
        window_idents = {
            window.ident
            for window in self.state.wnds
            if window.sess_ident == sess_ident
        }

        for run in self.state.prc_runs:

            if any(wnd_ident in window_idents for wnd_ident in run.wnds):
                run.duration += elapsed

    def list_window_idents(self, sess_ident: str) -> tuple[int, ...]:
        """Return stable identities of current windows in one login session."""
        return tuple(
            window.ident
            for window in self.state.wnds
            if window.sess_ident == sess_ident
        )

    def create_process_events(
        self,
        ended: Iterable[ProcessRun],
        min_duration: int,
        timestamp: int,
        actor: Actor = Actor.UNKNOWN,
    ) -> tuple[Event, ...]:
        """Report threshold crossings and completed application runs."""
        return _create_process_events(
            self,
            ended,
            min_duration,
            timestamp,
            actor,
        )

    def session_run(self, sess_ident: str) -> SessionRun | None:
        """Return the active statistical run for a login session."""
        return next(
            (entry for entry in self.state.sess_runs if entry.sess_ident == sess_ident),
            None,
        )

    def active_session_usages(self, sess_ident: str) -> tuple[SessionUsage, ...]:
        """Return active quota usages for a login session across occurrences."""
        return tuple(
            usage
            for occurrence in self.state.occurrences
            for usage in occurrence.sessions
            if usage.ident == sess_ident and usage.active
        )

    def latest_break_duration(self, sess_ident: str) -> int:
        """Return the latest active usage's configured mandatory break."""
        usages = self.active_session_usages(sess_ident)

        if not usages:
            return 0

        return max(usages, key=lambda usage: usage.started).break_duration

    def request_session_termination(
        self,
        usage: SessionUsage,
        timestamp: int,
    ) -> bool:
        """Record the first quota-usage termination request."""
        require_nonnegative(timestamp, "session termination timestamp")

        if usage.terminate_requested is not None:
            return False

        usage.terminate_requested = timestamp
        return True

    def finish_session(self, usage: SessionUsage, timestamp: int) -> int | None:
        """Finish a session usage and extend the account break when required."""
        require_nonnegative(timestamp, "session finish timestamp")

        if not usage.active:
            return self.state.break_till

        usage.active = False
        forced = usage.terminate_requested is not None
        voluntary_threshold_reached = (
            usage.max_duration is not None
            and usage.spent * 5 > usage.max_duration * 4
        )

        if (forced or voluntary_threshold_reached) and usage.break_duration > 0:
            candidate = timestamp + usage.break_duration
            self.state.break_till = max(self.state.break_till or 0, candidate)

        return self.state.break_till

    def break_remaining(self, timestamp: int) -> int:
        """Return whole seconds remaining in the account-wide mandatory break."""
        require_nonnegative(timestamp, "break timestamp")
        return max(0, (self.state.break_till or 0) - timestamp)

    def suspend_collection(self) -> None:
        """Discard live observations without creating events or spending quotas."""
        self.state.sess_runs = []
        self.state.prc_runs = []
        self.state.wnds = []

        for occurrence in self.state.occurrences:

            for usage in occurrence.sessions:

                if usage.active:
                    usage.active = False
                    usage.terminate_requested = None

            for usage in occurrence.apps:
                usage.active_wnds = []

    def clear_active_windows(self) -> None:
        """Clear current application matches without changing spent quotas."""

        for occurrence in self.state.occurrences:

            for usage in occurrence.apps:
                usage.active_wnds = []
