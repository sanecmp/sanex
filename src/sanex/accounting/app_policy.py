"""Unified per-account application quota evaluation."""

from dataclasses import dataclass, field

from ..exceptions import AccountingError
from sanelib.protocol import Account, AppRule
from ..model.state import RangeOccurrence
from .app import AppQuota, AppRuleMatcher
from .process import ResolvedWindow
from .runtime import AccountRuntime
from .schedule import ScheduleResolver
from .validation import require_nonnegative


@dataclass(frozen=True, slots=True)
class BlockedWindow:
    """One concrete window blocked by one or more application rules."""

    window: ResolvedWindow
    rule_idents: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AppPolicyDecision:
    """All concrete windows that must be closed after one complete snapshot."""

    blocked: tuple[BlockedWindow, ...] = ()


@dataclass(slots=True)
class AppPolicyEvaluator:
    """Evaluate application policy using one configuration-bound resolver."""

    runtime: AccountRuntime
    resolver: ScheduleResolver
    _matchers: dict[int, tuple[AppRule, AppRuleMatcher]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def evaluate(
        self,
        account: Account,
        windows: tuple[ResolvedWindow, ...],
        unavailable_wnds: tuple[int, ...],
        timestamp: int,
        elapsed: int,
    ) -> AppPolicyDecision:
        """Update current AppUsage values and return concrete blocked windows."""
        require_nonnegative(elapsed, "application policy elapsed time")
        by_ident = {window.state.ident: window for window in windows}

        if len(by_ident) != len(windows):
            raise AccountingError("application snapshot contains duplicate window ident")

        unavailable = frozenset(unavailable_wnds)

        if len(unavailable) != len(unavailable_wnds) or any(
            not isinstance(ident, int) or isinstance(ident, bool) or ident < 0
            for ident in unavailable
        ):
            raise AccountingError("unavailable window ident is invalid")

        if unavailable & by_ident.keys():
            raise AccountingError("window cannot be both observed and unavailable")

        context = self._quota_context(account, timestamp)

        if context is None:
            return AppPolicyDecision()

        occurrence, rules = context

        blocked_rules: dict[int, set[int]] = {}

        for rule in rules:
            quota = AppQuota(occurrence, rule)
            quota.spend(elapsed)
            matcher = self._matcher(rule)
            matching = [
                window.state.ident
                for window in windows
                if matcher.matches(
                    window.process.prc_name,
                    window.process.exe,
                    window.observed.title,
                )
            ]
            usage = quota.usage

            if usage is not None:
                matching.extend(
                    wnd_ident
                    for wnd_ident in usage.active_wnds
                    if wnd_ident in unavailable
                )

            check = quota.reconcile(matching)

            for wnd_ident in check.blocked_wnds:

                if wnd_ident in by_ident:
                    blocked_rules.setdefault(wnd_ident, set()).add(rule.ident)

        return AppPolicyDecision(
            blocked=tuple(
                BlockedWindow(
                    window=by_ident[wnd_ident],
                    rule_idents=tuple(sorted(rule_idents)),
                )
                for wnd_ident, rule_idents in sorted(blocked_rules.items())
            )
        )

    def _quota_context(
        self,
        account: Account,
        timestamp: int,
    ) -> tuple[RangeOccurrence, tuple[AppRule, ...]] | None:

        if not account.apply:
            self.runtime.clear_active_windows()
            return None

        if account.limits is None:
            raise AccountingError("applied account has no limits")

        selected = self.resolver.resolve(account.limits, timestamp)

        if selected is None or not selected.session_rule.apply:
            self.runtime.clear_active_windows()
            return None

        occurrence = self.runtime.ensure_occurrence(selected)

        for other in self.runtime.state.occurrences:

            if other is not occurrence:

                for usage in other.apps:
                    usage.active_wnds = []

        rules = selected.session_rule.app_rules
        configured = {rule.ident for rule in rules}

        for usage in occurrence.apps:

            if usage.ident not in configured:
                usage.active_wnds = []

        return occurrence, rules

    def _matcher(self, rule: AppRule) -> AppRuleMatcher:
        cached = self._matchers.get(rule.ident)

        if cached is None or cached[0] != rule:
            cached = (rule, AppRuleMatcher(rule))
            self._matchers[rule.ident] = cached

        return cached[1]
