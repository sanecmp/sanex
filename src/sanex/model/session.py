"""Immutable systemd-logind session snapshots."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .validation import NonEmptyString, NonNegativeInt


class SessionType(StrEnum):
    """Supported local graphical session types."""

    X11 = "x11"
    WAYLAND = "wayland"


class SessionState(StrEnum):
    """States reported for a logind session."""

    OPENING = "opening"
    ONLINE = "online"
    ACTIVE = "active"
    CLOSING = "closing"


class LoginSession(BaseModel):
    """One normal graphical login session relevant to sanex."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ident: NonEmptyString
    uid: NonNegativeInt
    login: NonEmptyString
    path: NonEmptyString
    started: NonNegativeInt
    type: SessionType
    state: SessionState
    active: bool
    idle: bool
    locked: bool

    def is_ready(self) -> bool:
        """Whether setup has finished and shutdown has not started, even if inactive."""
        return self.state in (SessionState.ONLINE, SessionState.ACTIVE)
