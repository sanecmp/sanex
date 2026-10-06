"""Account session policy evaluation and persistent termination decisions."""

from dataclasses import dataclass
from enum import StrEnum

from ..exceptions import AccountingError
from sanelib.protocol import Account
from ..model.state import SessionRun, SessionUsage
from .runtime import AccountRuntime
from .schedule import ScheduleResolver
from .session import SessionQuota
from .validation import require_nonnegative


class SessionDenial(StrEnum):
    """Reason why one observed login session must be terminated."""

    MANDATORY_BREAK = "mandatory_break"
    OUTSIDE_RANGE = "outside_range"
    MAX_SESSIONS = "max_sessions"
    MAX_DURATION = "max_duration"


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """Current policy result and whether a new terminate call is required."""

    allowed: bool
    reason: SessionDenial | None = None
    terminate: bool = False
    usage: SessionUsage | None = None


@dataclass(frozen=True, slots=True)
class SessionPolicyEvaluator:
    """Evaluate session policy using one configuration-bound schedule resolver."""

    runtime: AccountRuntime
    resolver: ScheduleResolver

    def evaluate(
        self,
        account: Account,
        sess_ident: str,
        timestamp: int,
        elapsed: int,
    ) -> SessionDecision:
        """Apply current account schedule and quotas to one live session."""
        require_nonnegative(timestamp, "policy timestamp")
        require_nonnegative(elapsed, "policy elapsed time")
        run = self.runtime.session_run(sess_ident)

        if run is None:
            raise AccountingError(f"session {sess_ident} has no active SessionRun")

        if not account.apply:
            return SessionDecision(allowed=True)

        if account.limits is None:
            raise AccountingError("applied account has no limits")

        remaining_break = self.runtime.break_remaining(timestamp)

        if remaining_break > 0:
            return _deny(run, timestamp, 0, SessionDenial.MANDATORY_BREAK)

        selected = self.resolver.resolve(account.limits, timestamp)

        if selected is None:
            break_duration = self.runtime.latest_break_duration(sess_ident)
            return _deny(run, timestamp, break_duration, SessionDenial.OUTSIDE_RANGE)

        if not selected.session_rule.apply:
            return SessionDecision(allowed=True)

        occurrence = self.runtime.ensure_occurrence(selected)
        quota = SessionQuota(occurrence, selected.session_rule)
        existing_usage = quota.usage(sess_ident)
        check = quota.start(sess_ident, timestamp)

        if check.limit is not None:
            return _deny(
                run,
                timestamp,
                selected.session_rule.break_duration,
                SessionDenial(check.limit.value),
                usage=check.usage,
            )

        usage = check.usage

        if usage is not None and usage is existing_usage:
            limit = quota.spend(usage, elapsed)

            if limit is not None:
                return _deny(
                    run,
                    timestamp,
                    selected.session_rule.break_duration,
                    SessionDenial(limit.value),
                    usage=usage,
                )

        return SessionDecision(allowed=True, usage=usage)


def _deny(
    run: SessionRun,
    timestamp: int,
    break_duration: int,
    reason: SessionDenial,
    usage: SessionUsage | None = None,
) -> SessionDecision:
    terminate = run.terminate_requested is None

    if terminate:
        run.terminate_requested = timestamp
        run.break_duration = break_duration

    return SessionDecision(
        allowed=False,
        reason=reason,
        terminate=terminate,
        usage=usage,
    )
