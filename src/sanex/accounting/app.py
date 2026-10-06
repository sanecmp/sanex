"""Application-rule matching and per-occurrence quota accounting."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

import regex
from sanelib.protocol import AppRule, MatchType

from ..exceptions import AccountingError
from ..model.state import AppUsage, RangeOccurrence
from .validation import require_nonnegative


_REGEX_MATCH_TIMEOUT = 0.005
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppRuleMatcher:
    """Prepared matcher for one immutable application rule."""

    rule: AppRule
    _prc_name_regex: regex.Pattern[str] | None = field(init=False, repr=False)
    _exe_regex: regex.Pattern[str] | None = field(init=False, repr=False)
    _wnd_title_regex: regex.Pattern[str] | None = field(init=False, repr=False)
    _timed_out_conditions: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        rule = self.rule
        object.__setattr__(
            self,
            "_prc_name_regex",
            _compile_regex(rule.prc_name, rule.prc_name_match),
        )
        object.__setattr__(
            self,
            "_exe_regex",
            _compile_regex(rule.exe, rule.exe_match),
        )
        object.__setattr__(
            self,
            "_wnd_title_regex",
            _compile_regex(rule.wnd_title, rule.wnd_title_match),
        )

    def matches(self, prc_name: str, exe: str, wnd_title: str) -> bool:
        """Match all configured conditions against current window attributes."""
        rule = self.rule

        if not rule.apply:
            return False

        return (
            self._matches_condition(
                "prc_name",
                prc_name,
                rule.prc_name,
                rule.prc_name_match,
                self._prc_name_regex,
            )
            and self._matches_condition(
                "exe",
                exe,
                rule.exe,
                rule.exe_match,
                self._exe_regex,
            )
            and self._matches_condition(
                "wnd_title",
                wnd_title,
                rule.wnd_title,
                rule.wnd_title_match,
                self._wnd_title_regex,
            )
        )

    def _matches_condition(
        self,
        name: str,
        actual: str,
        expected: str | None,
        match_type: MatchType | None,
        pattern: regex.Pattern[str] | None,
    ) -> bool:
        timed_out = self._timed_out_conditions

        if name in timed_out:
            return False

        try:
            return _matches(actual, expected, match_type, pattern)

        except TimeoutError:
            timed_out.add(name)
            logger.warning(
                "Application rule %s condition %s timed out and was disabled",
                self.rule.ident,
                name,
            )
            return False


def matching_app_rules(
    matchers: Iterable[AppRuleMatcher],
    prc_name: str,
    exe: str,
    wnd_title: str,
) -> tuple[AppRule, ...]:
    """Return every applied rule matching the same observed window."""
    return tuple(
        matcher.rule
        for matcher in matchers
        if matcher.matches(prc_name, exe, wnd_title)
    )


def _compile_regex(
    expected: str | None,
    match_type: MatchType | None,
) -> regex.Pattern[str] | None:

    if expected is not None and match_type is MatchType.REGEX:
        return regex.compile(expected)

    return None


def _matches(
    actual: str,
    expected: str | None,
    match_type: MatchType | None,
    pattern: regex.Pattern[str] | None,
) -> bool:

    if expected is None:
        return True

    if match_type is MatchType.EXACT:
        return actual == expected

    if match_type is MatchType.CONTAINS:
        return expected in actual

    if match_type is MatchType.REGEX:
        assert pattern is not None
        return pattern.search(actual, timeout=_REGEX_MATCH_TIMEOUT) is not None

    return False


@dataclass(frozen=True, slots=True)
class AppCheck:
    """Application usage and windows blocked by each quota."""

    usage: AppUsage | None
    launch_blocked: tuple[int, ...] = ()
    time_blocked: tuple[int, ...] = ()

    @property
    def allowed(self) -> bool:
        """Whether every currently matching window may remain open."""
        return not self.launch_blocked and not self.time_blocked

    @property
    def blocked_wnds(self) -> tuple[int, ...]:
        """Return each blocked window once in stable order."""
        return tuple(dict.fromkeys((*self.launch_blocked, *self.time_blocked)))


@dataclass(frozen=True, slots=True)
class AppQuota:
    """Application quota bound to one configured range occurrence."""

    occurrence: RangeOccurrence
    rule: AppRule

    @property
    def usage(self) -> AppUsage | None:
        """Return previously recorded usage for this rule, if any."""
        return next(
            (entry for entry in self.occurrence.apps if entry.ident == self.rule.ident),
            None,
        )

    def reconcile(self, matching_wnds: Iterable[int]) -> AppCheck:
        """Reconcile all currently matching windows for this rule."""
        observed = tuple(matching_wnds)

        if any(
            not isinstance(ident, int) or isinstance(ident, bool) or ident < 0
            for ident in observed
        ):
            raise AccountingError("matching window ident is invalid")

        current = tuple(sorted(set(observed)))
        usage = self.usage

        if not self.rule.apply:

            if usage is not None:
                usage.active_wnds = []

            return AppCheck(usage=usage)

        if usage is None:

            if not current:
                return AppCheck(usage=None)

            usage = AppUsage(
                ident=self.rule.ident,
                spent=0,
                wnds=[],
                active_wnds=[],
            )
            self.occurrence.apps.append(usage)

        known = set(usage.wnds)
        usage.wnds.extend(ident for ident in current if ident not in known)
        usage.active_wnds = list(current)
        return _check_app_limits(usage, self.rule)

    def spend(self, seconds: int) -> AppCheck:
        """Spend active time once, regardless of the active window count."""
        require_nonnegative(seconds, "application elapsed time")
        usage = self.usage

        if usage is None:
            return AppCheck(usage=None)

        if self.rule.apply and usage.active_wnds:
            usage.spent += seconds

        return _check_app_limits(usage, self.rule)


def _check_app_limits(usage: AppUsage, rule: AppRule) -> AppCheck:

    if not rule.apply:
        return AppCheck(usage=usage)

    launch_blocked: tuple[int, ...] = ()

    if rule.max_launches is not None:
        allowed_wnds = set(usage.wnds[: rule.max_launches])
        launch_blocked = tuple(
            ident for ident in usage.active_wnds if ident not in allowed_wnds
        )

    time_blocked: tuple[int, ...] = ()

    if rule.max_time is not None and usage.spent >= rule.max_time:
        time_blocked = tuple(usage.active_wnds)

    return AppCheck(
        usage=usage,
        launch_blocked=launch_blocked,
        time_blocked=time_blocked,
    )
