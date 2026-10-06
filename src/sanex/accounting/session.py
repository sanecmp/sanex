"""Per-occurrence login-session quota accounting."""

from dataclasses import dataclass
from enum import StrEnum

from ..exceptions import AccountingError
from sanelib.protocol import SessionRule
from ..model.state import RangeOccurrence, SessionUsage
from .validation import require_nonnegative


class SessionLimit(StrEnum):
    """Session quota whose allowance has been exhausted."""

    MAX_SESSIONS = "max_sessions"
    MAX_DURATION = "max_duration"


@dataclass(frozen=True, slots=True)
class SessionCheck:
    """Result of reconciling one observed login session."""

    usage: SessionUsage | None
    limit: SessionLimit | None

    @property
    def allowed(self) -> bool:
        """Whether the session may continue under the current rule."""
        return self.limit is None


@dataclass(frozen=True, slots=True)
class SessionQuota:
    """Session quota bound to one configured range occurrence."""

    occurrence: RangeOccurrence
    rule: SessionRule

    def usage(self, sess_ident: str) -> SessionUsage | None:
        """Return previously recorded usage for a login session, if any."""
        return next(
            (entry for entry in self.occurrence.sessions if entry.ident == sess_ident),
            None,
        )

    def start(self, sess_ident: str, timestamp: int) -> SessionCheck:
        """Count a session once and immediately evaluate current limits."""

        if not sess_ident:
            raise AccountingError("session ident is empty")

        require_nonnegative(timestamp, "session start timestamp")
        usage = self.usage(sess_ident)

        if usage is None:

            if not self.rule.apply:
                return SessionCheck(usage=None, limit=None)

            limit = _new_session_limit(self.occurrence, self.rule)

            if limit is not None:
                return SessionCheck(usage=None, limit=limit)

            usage = SessionUsage(
                ident=sess_ident,
                started=timestamp,
                spent=0,
                active=True,
                max_duration=self.rule.max_duration,
                break_duration=self.rule.break_duration,
                terminate_requested=None,
            )
            self.occurrence.sessions.append(usage)

        else:
            usage.active = True
            _update_snapshot(usage, self.rule)

        return SessionCheck(
            usage=usage,
            limit=_current_limit(self.occurrence, self.rule, usage),
        )

    def spend(self, usage: SessionUsage, seconds: int) -> SessionLimit | None:
        """Add active seconds and return an exhausted limit, if any."""
        require_nonnegative(seconds, "session elapsed time")

        if not any(entry is usage for entry in self.occurrence.sessions):
            raise AccountingError("session usage does not belong to the range occurrence")

        _update_snapshot(usage, self.rule)

        if self.rule.apply:
            usage.spent += seconds

        return _current_limit(self.occurrence, self.rule, usage)


def _new_session_limit(
    occurrence: RangeOccurrence,
    rule: SessionRule,
) -> SessionLimit | None:

    if rule.max_sessions is not None and len(occurrence.sessions) >= rule.max_sessions:
        return SessionLimit.MAX_SESSIONS

    if rule.max_duration is not None and rule.max_duration < 60:
        return SessionLimit.MAX_DURATION

    return None


def _current_limit(
    occurrence: RangeOccurrence,
    rule: SessionRule,
    usage: SessionUsage,
) -> SessionLimit | None:

    if not rule.apply:
        return None

    position = next(
        index
        for index, entry in enumerate(occurrence.sessions, start=1)
        if entry is usage
    )

    if rule.max_sessions is not None and position > rule.max_sessions:
        return SessionLimit.MAX_SESSIONS

    if rule.max_duration is not None and (
        rule.max_duration < 60 or usage.spent >= rule.max_duration
    ):
        return SessionLimit.MAX_DURATION

    return None


def _update_snapshot(usage: SessionUsage, rule: SessionRule) -> None:
    usage.max_duration = rule.max_duration
    usage.break_duration = rule.break_duration
