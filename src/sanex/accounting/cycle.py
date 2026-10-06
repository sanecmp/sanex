"""Complete accounting and enforcement cycle for one local account."""

from dataclasses import dataclass, field
from typing import Protocol

from sanelib.protocol import Account, Config, Event

from ..enforcement.app import AppEnforcer, WindowCloser
from ..enforcement.session import SessionEnforcer, SessionTerminator, StateSaver
from ..exceptions import AccountingError
from ..model.session import LoginSession
from ..model.window import AtspiWindowSnapshot
from ..platform.process import WindowProcessResolver
from .app_policy import AppPolicyDecision, AppPolicyEvaluator
from .policy import SessionDecision, SessionPolicyEvaluator
from .runtime import AccountRuntime
from .schedule import ScheduleResolver
from .validation import require_nonnegative


class EventAppender(Protocol):
    """Append-only event-journal interface used by an account cycle."""

    def append(self, uid: int, event: Event) -> object: ...


@dataclass(frozen=True, slots=True)
class SessionObservation:
    """One current login session and its available AT-SPI snapshot."""

    session: LoginSession
    snapshot: AtspiWindowSnapshot | None


@dataclass(frozen=True, slots=True)
class SessionCycleDecision:
    """Policy decision associated with one concrete login session."""

    sess_ident: str
    decision: SessionDecision


@dataclass(frozen=True, slots=True)
class AccountCycleResult:
    """Observable result of one completed account-processing cycle."""

    events: tuple[Event, ...]
    session_decisions: tuple[SessionCycleDecision, ...]
    app_decision: AppPolicyDecision
    saved: bool


@dataclass(slots=True)
class AccountCycle:
    """Orchestrate observation, accounting, enforcement and persistence."""

    runtime: AccountRuntime
    config: Config
    process_resolver: WindowProcessResolver
    terminator: SessionTerminator
    window_closer: WindowCloser
    event_appender: EventAppender
    state_saver: StateSaver
    enforcement_enabled: bool = True
    _account: Account = field(init=False, repr=False)
    _session_policy: SessionPolicyEvaluator = field(init=False, repr=False)
    _app_policy: AppPolicyEvaluator = field(init=False, repr=False)
    _session_enforcer: SessionEnforcer = field(init=False, repr=False)
    _app_enforcer: AppEnforcer = field(init=False, repr=False)
    _unsaved_elapsed: int = field(default=0, init=False, repr=False)
    _saved_once: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        account = next(
            (entry for entry in self.config.accounts if entry.uid == self.runtime.uid),
            None,
        )

        if account is None:
            raise AccountingError(
                f"configuration has no account for UID {self.runtime.uid}"
            )

        if self.runtime.state.config_ident != self.config.ident:
            raise AccountingError("runtime state does not match configuration ident")

        resolver = ScheduleResolver.for_config(self.config)
        self._account = account
        self._session_policy = SessionPolicyEvaluator(self.runtime, resolver)
        self._app_policy = AppPolicyEvaluator(self.runtime, resolver)
        self._session_enforcer = SessionEnforcer(
            self.runtime,
            self.terminator,
            self.state_saver,
        )
        self._app_enforcer = AppEnforcer(self.runtime, self.window_closer)

    @property
    def collect(self) -> bool:
        """Whether this account currently requires window observation."""
        return self._account.collect

    async def run(
        self,
        observations: tuple[SessionObservation, ...],
        timestamp: int,
        elapsed: int,
        existing: bool = False,
    ) -> AccountCycleResult:
        """Process one complete, internally consistent account snapshot."""
        require_nonnegative(timestamp, "account cycle timestamp")
        require_nonnegative(elapsed, "account cycle elapsed time")

        if not isinstance(existing, bool):
            raise AccountingError("existing account-cycle flag is invalid")

        observations = tuple(sorted(observations, key=lambda item: item.session.ident))
        self._validate_observations(observations)
        self._unsaved_elapsed += elapsed

        if not self._account.collect:
            self.runtime.suspend_collection()
            saved = self._save_if_due()
            return AccountCycleResult(
                events=(),
                session_decisions=(),
                app_decision=AppPolicyDecision(),
                saved=saved,
            )

        sessions = tuple(item.session for item in observations)
        session_result = self.runtime.reconcile_sessions(
            sessions,
            timestamp,
            elapsed,
            existing,
        )
        observations_by_ident = {
            item.session.ident: item
            for item in observations
        }
        session_idents = set(observations_by_ident)
        session_idents.update(window.sess_ident for window in self.runtime.state.wnds)
        resolved_windows = []
        unavailable_wnds = []
        ended_processes = []

        for sess_ident in sorted(session_idents):
            observation = observations_by_ident.get(sess_ident)

            if observation is not None and observation.snapshot is None:
                unavailable_wnds.extend(self.runtime.list_window_idents(sess_ident))
                self.runtime.advance_unobserved_processes(sess_ident, elapsed)
                continue

            snapshot = AtspiWindowSnapshot()
            groups = ()

            if observation is not None:
                assert observation.snapshot is not None
                snapshot = observation.snapshot
                groups = self.process_resolver.resolve(
                    snapshot.windows,
                    self.runtime.uid,
                    self.config.ignored_prcs,
                )

            process_result = self.runtime.reconcile_windows(
                sess_ident,
                groups,
                snapshot,
                timestamp,
                elapsed,
            )

            if not snapshot.complete:
                unavailable_wnds.extend(process_result.unavailable_wnds)

            resolved_windows.extend(process_result.windows)
            ended_processes.extend(process_result.ended)

        events = [*session_result.events]
        events.extend(
            self.runtime.create_process_events(
                ended_processes,
                self.config.min_prc_duration,
                timestamp,
            )
        )
        self._append_events(events)

        decisions = tuple(
            SessionCycleDecision(
                sess_ident=observation.session.ident,
                decision=self._session_policy.evaluate(
                    self._account,
                    observation.session.ident,
                    timestamp,
                    elapsed,
                ),
            )
            for observation in observations
        )
        app_decision = self._app_policy.evaluate(
            self._account,
            tuple(resolved_windows),
            tuple(unavailable_wnds),
            timestamp,
            elapsed,
        )

        saved = False

        if self.enforcement_enabled:

            for item in decisions:

                if await self._session_enforcer.enforce(
                    item.sess_ident,
                    item.decision,
                ):
                    saved = True

            if saved:
                self._mark_saved()

            enforcement_events = await self._app_enforcer.enforce(
                app_decision,
                timestamp,
            )
            self._append_events(enforcement_events)
            events.extend(enforcement_events)

        if not saved:
            saved = self._save_if_due()

        return AccountCycleResult(
            events=tuple(events),
            session_decisions=decisions,
            app_decision=app_decision,
            saved=saved,
        )

    def _validate_observations(
        self,
        observations: tuple[SessionObservation, ...],
    ) -> None:
        idents = [item.session.ident for item in observations]

        if len(set(idents)) != len(idents):
            raise AccountingError("account snapshot contains duplicate session ident")

        foreign = next(
            (
                item.session
                for item in observations
                if item.session.uid != self.runtime.uid
            ),
            None,
        )

        if foreign is not None:
            raise AccountingError(
                f"session {foreign.ident} belongs to UID {foreign.uid}, "
                f"not {self.runtime.uid}"
            )

    def _append_events(self, events: tuple[Event, ...] | list[Event]) -> None:

        for event in events:
            self.event_appender.append(self.runtime.uid, event)

    def _save_if_due(self) -> bool:

        if self._saved_once and self._unsaved_elapsed < self.config.save_interval:
            return False

        self.state_saver.save(self.runtime.uid, self.runtime.state)
        self._mark_saved()
        return True

    def _mark_saved(self) -> None:
        self._saved_once = True
        self._unsaved_elapsed = 0
