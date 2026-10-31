"""Mutable per-account runtime state and JSON validation."""

from datetime import date
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sanelib.protocol import ApplicationIdentity, ObservationSource, UsageInterval

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


class _AccountedRun(_RuntimeModel):
    """Recorded duration and its known uninterrupted accounting intervals."""

    duration: NonNegativeInt
    intervals: list[UsageInterval] | None = None

    @model_validator(mode="after")
    def validate_intervals(self) -> Self:
        """Keep a complete interval history equal to the existing duration counter."""

        if self.intervals is not None and sum(interval.duration for interval in self.intervals) != self.duration:
            raise ValueError("interval durations must sum to duration")

        return self

    def advance_usage(self, timestamp: int, elapsed: int) -> None:
        """Locate newly counted seconds without filling gaps in accounting."""

        if not elapsed:
            return

        if self.intervals is not None:
            interval = UsageInterval(timestamp=timestamp, duration=elapsed)

            if self.intervals and self.intervals[-1].timestamp == timestamp - elapsed:
                interval = interval.model_copy(update={"duration": self.intervals[-1].duration + elapsed})
                self.intervals[-1] = interval

            else:
                self.intervals.append(interval)

        self.duration += elapsed


class SessionRun(_AccountedRun):
    """Active statistical observation and enforcement state of a login session."""

    run_ident: NonNegativeInt
    sess_ident: NonEmptyString
    started: NonNegativeInt
    terminate_requested: NonNegativeInt | None
    break_duration: NonNegativeInt

    @model_validator(mode="after")
    def validate_termination(self) -> Self:
        """Keep an unused break snapshot at zero until termination is requested."""

        if self.terminate_requested is None and self.break_duration != 0:
            raise ValueError("break_duration must be zero without terminate_requested")

        return self


class ProcessRun(_AccountedRun):
    """Active statistical observation of an application process group."""

    run_ident: NonNegativeInt | None
    group_ident: NonEmptyString
    sess_ident: NonEmptyString | None = None
    observed_via: ObservationSource = ObservationSource.ATSPI
    prc_name: NonEmptyString
    exe: NonEmptyString
    application: ApplicationIdentity | None = None
    application_update_pending: bool = False
    started: NonNegativeInt
    reported: bool
    wnds: list[NonNegativeInt]

    @model_validator(mode="after")
    def validate_reported_run(self) -> Self:
        """Keep event identity and reporting state consistent."""

        if self.reported != (self.run_ident is not None):
            raise ValueError("reported must be true exactly when run_ident is set")

        if self.application_update_pending and (not self.reported or self.application is None):
            raise ValueError("pending application update requires a reported application")

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
    source: ObservationSource = ObservationSource.ATSPI


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
    observed_at: NonNegativeInt | None = None
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
