"""Mutable per-account runtime state and JSON validation."""

from datetime import date
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..exceptions import RuntimeStateError
from .validation import (
    NonEmptyString,
    NonNegativeInt,
    decode_json_model,
    parse_json_model,
    require_unique,
)


PositiveInt = Annotated[int, Field(gt=0)]


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionRun(_RuntimeModel):
    """Active statistical observation and enforcement state of a login session."""

    run_ident: NonNegativeInt
    sess_ident: NonEmptyString
    started: NonNegativeInt
    duration: NonNegativeInt
    terminate_requested: NonNegativeInt | None
    break_duration: NonNegativeInt

    @model_validator(mode="after")
    def validate_termination(self) -> Self:
        """Keep an unused break snapshot at zero until termination is requested."""

        if self.terminate_requested is None and self.break_duration != 0:
            raise ValueError("break_duration must be zero without terminate_requested")

        return self


class ProcessRun(_RuntimeModel):
    """Active statistical observation of an application process group."""

    run_ident: NonNegativeInt | None
    group_ident: NonEmptyString
    prc_name: NonEmptyString
    exe: NonEmptyString
    started: NonNegativeInt
    duration: NonNegativeInt
    reported: bool
    wnds: list[NonNegativeInt]

    @model_validator(mode="after")
    def validate_reported_run(self) -> Self:
        """Keep event identity and reporting state consistent."""

        if self.reported != (self.run_ident is not None):
            raise ValueError("reported must be true exactly when run_ident is set")

        require_unique(self.wnds, "window ident")
        return self


class WindowState(_RuntimeModel):
    """Currently observed top-level window."""

    ident: NonNegativeInt
    sess_ident: NonEmptyString
    bus: NonEmptyString
    path: NonEmptyString
    pid: PositiveInt
    prc_started: NonNegativeInt


class SessionUsage(_RuntimeModel):
    """Session quota consumption in one range occurrence."""

    ident: NonEmptyString
    started: NonNegativeInt
    spent: NonNegativeInt
    active: bool
    max_duration: NonNegativeInt | None
    break_duration: NonNegativeInt
    terminate_requested: NonNegativeInt | None


class AppUsage(_RuntimeModel):
    """Application quota consumption in one range occurrence."""

    ident: NonNegativeInt
    spent: NonNegativeInt
    wnds: list[NonNegativeInt]
    active_wnds: list[NonNegativeInt]

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        """Validate counted and currently matching windows."""
        require_unique(self.wnds, "window ident")
        require_unique(self.active_wnds, "active window ident")
        unknown = set(self.active_wnds).difference(self.wnds)

        if unknown:
            raise ValueError(f"active_wnds contains uncounted window {min(unknown)}")

        return self


class RangeOccurrence(_RuntimeModel):
    """Quota consumption for one local occurrence of a configured range."""

    range_ident: NonNegativeInt
    date: date
    sessions: list[SessionUsage]
    apps: list[AppUsage]

    @model_validator(mode="after")
    def validate_usage_identities(self) -> Self:
        """Keep each session and application counter unique in an occurrence."""
        require_unique((session.ident for session in self.sessions), "session ident")
        require_unique((app.ident for app in self.apps), "AppRule ident")
        return self


class RuntimeState(_RuntimeModel):
    """Complete mutable runtime state for one local account."""

    config_ident: NonNegativeInt
    boot_ident: NonEmptyString
    wnd_seq: NonNegativeInt
    event_seq: NonNegativeInt
    break_till: NonNegativeInt | None
    sess_runs: list[SessionRun]
    prc_runs: list[ProcessRun]
    wnds: list[WindowState]
    occurrences: list[RangeOccurrence]

    @model_validator(mode="after")
    def validate_identities_and_sequences(self) -> Self:
        """Validate unique identities and monotonic sequence watermarks."""
        require_unique((run.sess_ident for run in self.sess_runs), "active session ident")
        run_idents = [run.run_ident for run in self.sess_runs]
        run_idents.extend(run.run_ident for run in self.prc_runs if run.run_ident is not None)
        require_unique(run_idents, "event run ident")
        require_unique((window.ident for window in self.wnds), "window ident")
        require_unique(
            ((occurrence.range_ident, occurrence.date) for occurrence in self.occurrences),
            "range occurrence",
        )

        if self.wnds and self.wnd_seq < max(window.ident for window in self.wnds):
            raise ValueError("wnd_seq is lower than an active window ident")

        if run_idents and self.event_seq < max(run_idents):
            raise ValueError("event_seq is lower than an active run ident")

        return self


def parse_runtime_state(data: str | bytes | bytearray) -> RuntimeState:
    """Decode and validate a complete JSON runtime state."""
    return parse_json_model(RuntimeState, data, RuntimeStateError)


def decode_runtime_state(value: object) -> RuntimeState:
    """Validate a JSON-compatible Python value as runtime state."""
    return decode_json_model(RuntimeState, value, RuntimeStateError)
