"""Selection of the currently applicable configured range."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Self
from zoneinfo import ZoneInfo

from ..exceptions import AccountingError
from sanelib.protocol import Config, Limits, Range, SessionRule


@dataclass(frozen=True, slots=True)
class ActiveRange:
    """A configured range occurrence selected for one local moment."""

    occurrence_date: date
    range: Range
    session_rule: SessionRule


@dataclass(frozen=True, slots=True)
class ScheduleResolver:
    """Resolve Unix timestamps using the configured local timezone."""

    timezone: ZoneInfo

    @classmethod
    def for_config(cls, config: Config) -> Self:
        """Create a resolver from a validated configuration snapshot."""
        return cls(timezone=ZoneInfo(config.timezone))

    def resolve(self, limits: Limits, timestamp: int) -> ActiveRange | None:
        """Return the applied half-open range containing the timestamp."""
        local = self._get_local_datetime(timestamp)
        minute = local.hour * 60 + local.minute
        selected = next(
            (
                entry
                for entry in limits.ranges
                if entry.apply
                and entry.weekday == local.weekday()
                and entry.since <= minute < entry.till
            ),
            None,
        )

        if selected is None:
            return None

        session_rule = next(
            entry for entry in limits.session_rules if entry.ident == selected.session_rule_ident
        )
        return ActiveRange(
            occurrence_date=local.date(),
            range=selected,
            session_rule=session_rule,
        )

    def calculate_remaining(self, selected: ActiveRange, timestamp: int) -> int:
        """Return whole seconds until the selected range occurrence ends."""
        self._get_local_datetime(timestamp)
        range_end = datetime.combine(
            selected.occurrence_date,
            time.min,
            self.timezone,
        ) + timedelta(minutes=selected.range.till)
        return max(0, int(range_end.timestamp()) - timestamp)

    def _get_local_datetime(self, timestamp: int) -> datetime:

        if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
            raise AccountingError("range timestamp is invalid")

        try:
            return datetime.fromtimestamp(timestamp, self.timezone)

        except (OverflowError, OSError, ValueError) as error:
            raise AccountingError(f"range timestamp is invalid: {error}") from error
