"""Tests for compact desktop-indicator presentation state."""

import pytest

from sanex.localization import user_gettext
from sanex.indicator.presentation import (
    PROJECT_URL,
    IndicatorIcon,
    IndicatorPresentationBuilder,
)
from sanex.model.indicator import IndicatorStatus


@pytest.fixture
def builder() -> IndicatorPresentationBuilder:
    return IndicatorPresentationBuilder(version="1.2.3")


def test_presents_unavailable_service_as_warning(
    builder: IndicatorPresentationBuilder,
) -> None:
    presentation = builder.build(None)

    assert presentation.icon is IndicatorIcon.WARNING
    assert presentation.status_label == "Sanex service is unavailable"
    assert presentation.tooltip == presentation.status_label
    assert presentation.break_label is None


def test_presents_account_without_limits(
    builder: IndicatorPresentationBuilder,
) -> None:
    presentation = builder.build(
        IndicatorStatus(remaining=None, break_duration=0)
    )

    assert presentation.icon is IndicatorIcon.NORMAL
    assert presentation.status_label == "No time limit"
    assert presentation.break_label is None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 min"),
        (1, "1 min"),
        (60, "1 min"),
        (3600, "1 h"),
        (3661, "1 h 2 min"),
    ],
)
def test_formats_duration_rounded_up_to_minutes(
    builder: IndicatorPresentationBuilder,
    seconds: int,
    expected: str,
) -> None:
    assert builder.format_duration(seconds) == expected


def test_presents_remaining_time_break_and_about_information(
    builder: IndicatorPresentationBuilder,
) -> None:
    presentation = builder.build(
        IndicatorStatus(remaining=3661, break_duration=7200)
    )

    assert presentation.status_label == "Time remaining: 1 h 2 min"
    assert presentation.break_label == "Required break: 2 h"
    assert presentation.about_label == "About sanex"
    assert presentation.about.title == "About sanex"
    assert presentation.about.version == "Version 1.2.3"
    assert presentation.about.project_label == "A component of the sanecmp project"
    assert presentation.about.project_url == PROJECT_URL


def test_translates_labels_and_duration_units() -> None:
    translations = {
        "About sanex": "О программе",
        "Version": "Версия",
        "A component of the sanecmp project": "Компонент проекта sanecmp",
        "Time remaining": "Осталось времени",
        "Required break": "Обязательный перерыв",
        "h": "ч",
        "min": "мин",
    }
    builder = IndicatorPresentationBuilder(
        version="1.2.3",
        translate=lambda message: translations.get(message, message),
    )

    presentation = builder.build(
        IndicatorStatus(remaining=3661, break_duration=7200)
    )

    assert presentation.status_label == "Осталось времени: 1 ч 2 мин"
    assert presentation.break_label == "Обязательный перерыв: 2 ч"
    assert presentation.about.version == "Версия 1.2.3"


def test_uses_desktop_user_language_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGUAGE", "ru_RU")
    builder = IndicatorPresentationBuilder(
        version="1.2.3",
        translate=user_gettext(),
    )

    presentation = builder.build(
        IndicatorStatus(remaining=3661, break_duration=7200)
    )

    assert presentation.status_label == "Осталось времени: 1 ч 2 мин"
    assert presentation.break_label == "Обязательный перерыв: 2 ч"
    assert presentation.about.version == "Версия 1.2.3"
