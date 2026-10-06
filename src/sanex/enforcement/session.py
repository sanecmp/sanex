"""Execution and durable recording of logind session termination decisions."""

import logging
from dataclasses import dataclass
from typing import Protocol

from ..accounting.policy import SessionDecision
from ..accounting.runtime import AccountRuntime
from ..exceptions import AccountingError
from ..model.state import RuntimeState


logger = logging.getLogger(__name__)


class SessionTerminator(Protocol):
    """Minimal logind termination interface."""

    async def terminate(self, ident: str) -> None: ...


class StateSaver(Protocol):
    """Persistent runtime-state interface used after enforcement."""

    def save(self, uid: int, state: RuntimeState) -> RuntimeState: ...


@dataclass(frozen=True, slots=True)
class SessionEnforcer:
    """Persist and terminate exactly one denied login session."""

    runtime: AccountRuntime
    terminator: SessionTerminator
    state_saver: StateSaver

    async def enforce(
        self,
        sess_ident: str,
        decision: SessionDecision,
    ) -> bool:
        """Execute a new denial decision and return whether a command was sent."""

        if decision.allowed or not decision.terminate:
            return False

        run = self.runtime.session_run(sess_ident)

        if run is None or run.terminate_requested is None:
            raise AccountingError("session termination decision has no persistent request")

        requested = run.terminate_requested
        break_duration = run.break_duration
        self.state_saver.save(self.runtime.uid, self.runtime.state)
        try:
            await self.terminator.terminate(sess_ident)

        except Exception:

            if run.terminate_requested == requested and run.break_duration == break_duration:
                run.terminate_requested = None
                run.break_duration = 0
                self.state_saver.save(self.runtime.uid, self.runtime.state)

            raise

        logger.info(
            "Requested termination of session %s for UID %d", sess_ident, self.runtime.uid,
        )
        return True
