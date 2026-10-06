"""User-visible state derived from session accounting data."""

from sanelib.protocol import Account

from dataclasses import dataclass

from ..accounting.runtime import AccountRuntime
from ..accounting.schedule import ScheduleResolver
from ..model.indicator import IndicatorStatus


@dataclass(frozen=True, slots=True)
class IndicatorStatusBuilder:
    """Build indicator state for one account runtime and configuration."""

    runtime: AccountRuntime
    resolver: ScheduleResolver

    def build(self, account: Account, sess_ident: str, timestamp: int) -> IndicatorStatus:
        """Return the effective current-session allowance and future break."""

        if not account.apply:
            return IndicatorStatus(remaining=None, break_duration=0)

        limits = account.limits

        if limits is None:
            return IndicatorStatus(remaining=0, break_duration=0)

        selected = self.resolver.resolve(limits, timestamp)

        if selected is None:
            return IndicatorStatus(remaining=0, break_duration=0)

        remaining = self.resolver.calculate_remaining(selected, timestamp)
        rule = selected.session_rule

        if not rule.apply:
            return IndicatorStatus(remaining=remaining, break_duration=0)

        occurrence = self.runtime.find_occurrence(
            selected.range.ident,
            selected.occurrence_date,
        )
        usage = next(
            (
                entry
                for entry in occurrence.sessions
                if entry.ident == sess_ident and entry.active
            ),
            None,
        ) if occurrence is not None else None
        max_duration = rule.max_duration

        if max_duration is not None:
            spent = usage.spent if usage is not None else 0
            remaining = min(remaining, max(0, max_duration - spent))

        return IndicatorStatus(
            remaining=remaining,
            break_duration=rule.break_duration,
        )
