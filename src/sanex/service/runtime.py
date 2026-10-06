"""Persistent per-account runtime preparation across service restarts and configs."""

from dataclasses import dataclass

from ..exceptions import ServiceError
from sanelib.protocol import Account, SessionRule
from ..model.state import RuntimeState
from ..storage.events import EventStore
from ..storage.state import RuntimeStateStore


@dataclass(frozen=True, slots=True)
class RuntimeRepository:
    """Load, reconcile and durably save account runtime state."""

    state_store: RuntimeStateStore
    event_store: EventStore
    boot_ident: str

    def load(self, account: Account, config_ident: int) -> RuntimeState:
        """Load one state and reconcile it with the current boot and config."""
        state = self.state_store.load(account.uid)
        changed = False

        if state is None:
            state = self._new_state(config_ident)
            changed = True

        if state.boot_ident != self.boot_ident:
            self._reset_active_state(state)
            changed = True

        if state.config_ident != config_ident:
            self._apply_account_config(state, account, config_ident)
            changed = True

        event_seq = self.event_store.recover(account.uid, state.event_seq)

        if event_seq != state.event_seq:
            state.event_seq = event_seq
            changed = True

        if changed:
            self.state_store.save(account.uid, state)

        return state

    def apply(
        self,
        uid: int,
        state: RuntimeState,
        account: Account,
        config_ident: int,
    ) -> None:
        """Apply a new account config to live state and save it immediately."""

        if uid != account.uid:
            raise ServiceError(
                f"runtime UID {uid} does not match account UID {account.uid}"
            )

        self._apply_account_config(state, account, config_ident)
        self.state_store.save(uid, state)

    def _new_state(self, config_ident: int) -> RuntimeState:
        return RuntimeState(
            config_ident=config_ident,
            boot_ident=self.boot_ident,
            wnd_seq=0,
            event_seq=0,
            break_till=None,
            sess_runs=[],
            prc_runs=[],
            wnds=[],
            occurrences=[],
        )

    def _reset_active_state(self, state: RuntimeState) -> None:
        state.boot_ident = self.boot_ident
        state.sess_runs = []
        state.prc_runs = []
        state.wnds = []

        for occurrence in state.occurrences:

            for usage in occurrence.sessions:
                usage.active = False
                usage.terminate_requested = None

            for usage in occurrence.apps:
                usage.active_wnds = []

    @staticmethod
    def _apply_account_config(
        state: RuntimeState,
        account: Account,
        config_ident: int,
    ) -> None:
        state.config_ident = config_ident
        range_rules: dict[int, SessionRule] = {}

        if account.limits is not None:
            rules = {rule.ident: rule for rule in account.limits.session_rules}
            range_rules = {
                entry.ident: rules[entry.session_rule_ident]
                for entry in account.limits.ranges
            }

        for occurrence in state.occurrences:
            rule = range_rules.get(occurrence.range_ident)

            if rule is not None:

                for usage in occurrence.sessions:

                    if usage.active:
                        usage.max_duration = rule.max_duration
                        usage.break_duration = rule.break_duration

            for usage in occurrence.apps:
                usage.active_wnds = []
