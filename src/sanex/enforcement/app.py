"""Best-effort closing of concrete windows blocked by application rules."""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from sanelib.protocol import EmptyMeta, EnforcementFailedEvent, EnforcementFailureReason, EventType

from ..accounting.app_policy import AppPolicyDecision, BlockedWindow
from ..accounting.runtime import AccountRuntime
from ..accounting.validation import require_nonnegative
from ..exceptions import AccountingError
from ..model.window import AtspiWindow


logger = logging.getLogger(__name__)


class WindowCloser(Protocol):
    """Minimal interface for requesting closure of one concrete window."""

    async def close_window(
        self,
        uid: int,
        sess_ident: str,
        window: AtspiWindow,
    ) -> bool: ...


@dataclass(slots=True)
class _CloseAttempt:
    failure: EnforcementFailureReason | None
    reported_rules: set[int] = field(default_factory=set)


@dataclass(slots=True)
class AppEnforcer:
    """Request each window close once and verify it on the next snapshot."""

    runtime: AccountRuntime
    closer: WindowCloser
    _attempts: dict[tuple[int, int], _CloseAttempt] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    async def enforce(
        self,
        decision: AppPolicyDecision,
        timestamp: int,
    ) -> tuple[EnforcementFailedEvent, ...]:
        """Apply one complete policy snapshot and return newly created failures."""
        require_nonnegative(timestamp, "application enforcement timestamp")
        blocked_by_key = {
            (self.runtime.uid, blocked.window.state.ident): blocked
            for blocked in decision.blocked
        }

        if len(blocked_by_key) != len(decision.blocked):
            raise AccountingError("application decision contains duplicate window ident")

        current_keys = set(blocked_by_key)

        for key in tuple(self._attempts):

            if key[0] == self.runtime.uid and key not in current_keys:
                del self._attempts[key]

        events: list[EnforcementFailedEvent] = []

        for key, blocked in blocked_by_key.items():
            attempt = self._attempts.get(key)

            if attempt is None:
                failure = await self._request_close(blocked)
                attempt = _CloseAttempt(failure=failure)
                self._attempts[key] = attempt

            elif attempt.failure is None:
                attempt.failure = EnforcementFailureReason.CLOSE_TIMEOUT

            events.extend(self._failure_events(blocked, attempt, timestamp))

        return tuple(events)

    async def _request_close(
        self,
        blocked: BlockedWindow,
    ) -> EnforcementFailureReason | None:
        try:
            accepted = await self.closer.close_window(
                self.runtime.uid,
                blocked.window.state.sess_ident,
                blocked.window.observed,
            )

        except Exception as error:
            logger.warning(
                "Unable to close window %s for rules %s: %s",
                blocked.window.state.ident,
                blocked.rule_idents,
                error,
            )
            return EnforcementFailureReason.CLOSE_FAILED

        if not accepted:
            return EnforcementFailureReason.CLOSE_UNSUPPORTED

        return None

    def _failure_events(
        self,
        blocked: BlockedWindow,
        attempt: _CloseAttempt,
        timestamp: int,
    ) -> tuple[EnforcementFailedEvent, ...]:

        if attempt.failure is None:
            return ()

        events: list[EnforcementFailedEvent] = []

        for rule_ident in blocked.rule_idents:

            if rule_ident in attempt.reported_rules:
                continue

            event_seq = self.runtime.next_event_seq()
            events.append(
                EnforcementFailedEvent(
                    seq=event_seq,
                    type=EventType.ENFORCEMENT_FAILED,
                    timestamp=timestamp,
                    rule_ident=rule_ident,
                    wnd_ident=blocked.window.state.ident,
                    reason=attempt.failure,
                    meta=EmptyMeta(),
                )
            )
            attempt.reported_rules.add(rule_ident)

        return tuple(events)
