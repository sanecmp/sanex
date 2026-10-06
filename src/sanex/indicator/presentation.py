"""User-facing indicator labels independent of the desktop protocol."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from gettext import gettext

from ..model.indicator import IndicatorStatus


PROJECT_URL = "https://github.com/sanecmp"


class IndicatorIcon(StrEnum):
    """Symbolic icon selected for the current service state."""

    NORMAL = "sanex-symbolic"
    WARNING = "sanex-warning-symbolic"


@dataclass(frozen=True, slots=True)
class AboutPresentation:
    """Informative content displayed by the About action."""

    title: str
    version: str
    project_label: str
    project_url: str


@dataclass(frozen=True, slots=True)
class IndicatorPresentation:
    """Complete text and icon state consumed by a desktop adapter."""

    icon: IndicatorIcon
    tooltip: str
    status_label: str
    break_label: str | None
    about_label: str
    about: AboutPresentation


@dataclass(frozen=True, slots=True)
class IndicatorPresentationBuilder:
    """Build compact, translatable indicator presentation state."""

    version: str
    translate: Callable[[str], str] = gettext

    def build(self, status: IndicatorStatus | None) -> IndicatorPresentation:
        """Build labels for a service status or disconnected state."""
        translate = self.translate
        about_label = translate("About sanex")
        about = AboutPresentation(
            title=about_label,
            version=f"{translate("Version")} {self.version}",
            project_label=translate("A component of the sanecmp project"),
            project_url=PROJECT_URL,
        )

        if status is None:
            label = translate("Sanex service is unavailable")
            return IndicatorPresentation(
                icon=IndicatorIcon.WARNING,
                tooltip=label,
                status_label=label,
                break_label=None,
                about_label=about_label,
                about=about,
            )

        remaining = status.remaining

        if remaining is None:
            label = translate("No time limit")

        else:
            duration = self.format_duration(remaining)
            label = f"{translate("Time remaining")}: {duration}"

        break_duration = status.break_duration
        break_label = None

        if break_duration > 0:
            duration = self.format_duration(break_duration)
            break_label = f"{translate("Required break")}: {duration}"

        return IndicatorPresentation(
            icon=IndicatorIcon.NORMAL,
            tooltip=label,
            status_label=label,
            break_label=break_label,
            about_label=about_label,
            about=about,
        )

    def format_duration(self, seconds: int) -> str:
        """Format seconds as compact rounded-up hours and minutes."""
        minutes = (seconds + 59) // 60
        hours, minutes = divmod(minutes, 60)
        translate = self.translate
        chunks = []

        if hours > 0:
            chunks.append(f"{hours} {translate("h")}")

        if minutes > 0 or not chunks:
            chunks.append(f"{minutes} {translate("min")}")

        return " ".join(chunks)
