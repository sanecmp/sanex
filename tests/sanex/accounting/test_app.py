"""Tests for application-rule matching."""

from typing import Any

import pytest
from sanelib.protocol import MatchType

from sanex.accounting.app import AppRuleMatcher, matching_app_rules
from tests.sanex.accounting.factories import configured_app_rule


def test_filled_conditions_are_combined_with_and(config_payload: dict[str, Any]) -> None:
    matcher = AppRuleMatcher(configured_app_rule(config_payload))

    assert matcher.matches(
        "yandex_browser",
        "/opt/yandex/browser/yandex_browser",
        "Any title",
    )
    assert not matcher.matches(
        "other",
        "/opt/yandex/browser/yandex_browser",
        "Any title",
    )
    assert not matcher.matches(
        "yandex_browser",
        "/usr/bin/other",
        "Any title",
    )


@pytest.mark.parametrize(
    ("match_type", "condition", "title", "expected"),
    [
        (MatchType.EXACT, "School portal", "School portal", True),
        (MatchType.EXACT, "School portal", "School portal — homework", False),
        (MatchType.CONTAINS, "School portal", "School portal — homework", True),
        (MatchType.CONTAINS, "school portal", "School portal — homework", False),
        (MatchType.REGEX, r"^Lesson \d+$", "Lesson 42", True),
        (MatchType.REGEX, r"^Lesson \d+$", "Current Lesson 42", False),
    ],
)
def test_supported_match_types_are_case_sensitive(
    config_payload: dict[str, Any],
    match_type: MatchType,
    condition: str,
    title: str,
    expected: bool,
) -> None:
    rule = configured_app_rule(
        config_payload,
        prc_name=None,
        prc_name_match=None,
        exe=None,
        exe_match=None,
        wnd_title=condition,
        wnd_title_match=match_type,
    )

    assert AppRuleMatcher(rule).matches("process", "/usr/bin/process", title) is expected


def test_slow_regex_is_disabled_after_first_timeout(
    config_payload: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    rule = configured_app_rule(
        config_payload,
        prc_name=None,
        prc_name_match=None,
        exe=None,
        exe_match=None,
        wnd_title=r"(a+)+$",
        wnd_title_match=MatchType.REGEX,
    )
    matcher = AppRuleMatcher(rule)
    title = f"{"a" * 1_000}!"

    assert not matcher.matches("process", "/usr/bin/process", title)
    assert not matcher.matches("process", "/usr/bin/process", title)
    assert caplog.messages == [
        f"Application rule {rule.ident} condition wnd_title timed out and was disabled",
    ]


def test_all_matching_rules_are_returned_and_disabled_rules_are_skipped(
    config_payload: dict[str, Any],
) -> None:
    first = configured_app_rule(config_payload)
    second = configured_app_rule(config_payload, ident=702, name="Second rule")
    disabled = configured_app_rule(config_payload, ident=703, apply=False)
    matchers = tuple(AppRuleMatcher(rule) for rule in (first, second, disabled))

    matched = matching_app_rules(
        matchers,
        "yandex_browser",
        "/opt/yandex/browser/yandex_browser",
        "Any title",
    )

    assert matched == (first, second)
