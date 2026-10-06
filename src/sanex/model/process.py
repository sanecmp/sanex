"""Immutable Linux process identities used for application accounting."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .validation import NonEmptyString, NonNegativeInt


PositiveInt = Annotated[int, Field(gt=0)]


class ProcessIdentity(BaseModel):
    """A process identity stable against PID reuse during one boot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pid: PositiveInt
    parent_pid: NonNegativeInt
    uid: NonNegativeInt
    started: NonNegativeInt
    prc_name: NonEmptyString
    exe: NonEmptyString
    cgroup: NonEmptyString | None = None
