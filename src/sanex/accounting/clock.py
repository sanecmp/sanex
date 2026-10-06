"""Monotonic active-time accounting that excludes system sleep."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..exceptions import AccountingError


_NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(slots=True)
class ActiveTimeClock:
    """Return elapsed whole active seconds while preserving subsecond remainder."""

    now: Callable[[], int] = time.monotonic_ns
    _last: int = field(init=False, repr=False)
    _remainder: int = field(default=0, init=False, repr=False)
    _sleeping: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._last = self._read()

    @property
    def sleeping(self) -> bool:
        """Whether elapsed time is currently excluded from accounting."""
        return self._sleeping

    def advance(self) -> int:
        """Return new whole active seconds since the previous observation."""
        current = self._read()
        elapsed = current - self._last
        self._last = current

        if self._sleeping:
            return 0

        elapsed += self._remainder
        seconds, self._remainder = divmod(elapsed, _NANOSECONDS_PER_SECOND)
        return seconds

    def set_sleeping(self, sleeping: bool) -> int:
        """Apply a sleep transition and return active time preceding it."""
        elapsed = self.advance()
        self._sleeping = sleeping
        return elapsed

    def _read(self) -> int:
        current = self.now()

        if not isinstance(current, int) or isinstance(current, bool) or current < 0:
            raise AccountingError("monotonic clock returned an invalid value")

        if hasattr(self, "_last") and current < self._last:
            raise AccountingError("monotonic clock moved backwards")

        return current
