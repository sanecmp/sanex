"""System-wide orchestration of per-account accounting cycles."""

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from ..accounting.clock import ActiveTimeClock
from ..accounting.cycle import AccountCycle, AccountCycleResult, SessionObservation
from ..exceptions import AccountingError
from ..model.session import LoginSession
from ..model.window import AtspiWindowSnapshot


logger = logging.getLogger(__name__)


class SessionSource(Protocol):
    """Complete source of current graphical login sessions."""

    async def sessions(self) -> tuple[LoginSession, ...]: ...


class SessionWindowSource(Protocol):
    """Lifecycle and snapshots for per-session window observers."""

    async def reconcile(self, sessions: tuple[LoginSession, ...]) -> None: ...

    async def windows(self, session: LoginSession) -> AtspiWindowSnapshot: ...


@dataclass(frozen=True, slots=True)
class AccountWalkResult:
    """Result or isolated failure of one account cycle."""

    uid: int
    result: AccountCycleResult | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    """Results produced from one shared logind snapshot."""

    timestamp: int
    elapsed: int
    accounts: tuple[AccountWalkResult, ...]


@dataclass(slots=True)
class Supervisor:
    """Take one system snapshot and advance every configured account once."""

    session_source: SessionSource
    window_source: SessionWindowSource
    cycles: Mapping[int, AccountCycle]
    clock: ActiveTimeClock
    wall_clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:

        for uid, cycle in self.cycles.items():

            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                raise AccountingError("supervisor account UID is invalid")

            if cycle.runtime.uid != uid:
                raise AccountingError(
                    f"supervisor key UID {uid} does not match cycle UID "
                    f"{cycle.runtime.uid}"
                )

    async def walk(
        self,
        existing: bool = False,
        elapsed: int | None = None,
    ) -> SupervisorResult:
        """Advance all account cycles from one complete logind snapshot."""

        if not isinstance(existing, bool):
            raise AccountingError("existing supervisor flag is invalid")

        if elapsed is None:
            elapsed = self.clock.advance()

        elif not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0:
            raise AccountingError("supervisor elapsed time is invalid")

        timestamp = self._timestamp()
        sessions = await self.session_source.sessions()
        observations = await self._observe_windows(sessions)
        by_uid: dict[int, list[SessionObservation]] = {
            uid: []
            for uid in self.cycles
        }

        for observation in observations:
            account_observations = by_uid.get(observation.session.uid)

            if account_observations is not None:
                account_observations.append(observation)

        results: list[AccountWalkResult] = []

        for uid, cycle in sorted(self.cycles.items()):
            try:
                result = await cycle.run(
                    tuple(by_uid[uid]),
                    timestamp,
                    elapsed,
                    existing,
                )

            except Exception as error:
                logger.exception("Account cycle failed for UID %s", uid)
                results.append(AccountWalkResult(uid=uid, error=error))

            else:
                results.append(AccountWalkResult(uid=uid, result=result))

        return SupervisorResult(
            timestamp=timestamp,
            elapsed=elapsed,
            accounts=tuple(results),
        )

    async def _observe_windows(
        self,
        sessions: tuple[LoginSession, ...],
    ) -> tuple[SessionObservation, ...]:
        relevant = tuple(
            session
            for session in sessions
            if session.uid in self.cycles and self.cycles[session.uid].collect
        )
        await self.window_source.reconcile(relevant)
        snapshots = await asyncio.gather(
            *(self.window_source.windows(session) for session in relevant),
            return_exceptions=True,
        )
        observations: list[SessionObservation] = []

        for session, snapshot in zip(relevant, snapshots, strict=True):

            if isinstance(snapshot, asyncio.CancelledError):
                raise snapshot

            if isinstance(snapshot, Exception):
                logger.warning(
                    "Window observation failed for session %s of UID %s: %s",
                    session.ident,
                    session.uid,
                    snapshot,
                )
                observations.append(SessionObservation(session, None))

            else:
                observations.append(SessionObservation(session, snapshot))

        return tuple(observations)

    def _timestamp(self) -> int:
        value = self.wall_clock()

        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise AccountingError("wall clock returned an invalid value")

        return int(value)
