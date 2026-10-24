"""Immutable top-level window snapshots from AT-SPI."""

from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sanelib.protocol import ObservationSource

from .validation import NonEmptyString


PositiveInt = Annotated[int, Field(gt=0)]


class WindowRole(IntEnum):
    """AT-SPI roles treated as top-level application windows."""

    DIALOG = 16
    FRAME = 23
    WINDOW = 69


class AtspiWindow(BaseModel):
    """One top-level window and its source-specific address."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bus: NonEmptyString
    path: NonEmptyString
    pid: PositiveInt
    title: str
    role: WindowRole
    source: ObservationSource = ObservationSource.ATSPI


class AtspiWindowReference(BaseModel):
    """Identity of one top-level AT-SPI object that could not be read."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bus: NonEmptyString
    path: NonEmptyString


class AtspiWindowSnapshot(BaseModel):
    """Observed windows plus the exact inaccessible parts of the snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    windows: tuple[AtspiWindow, ...] = ()
    unavailable_buses: tuple[NonEmptyString, ...] = ()
    unavailable_windows: tuple[AtspiWindowReference, ...] = ()
    unavailable_sources: tuple[ObservationSource, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every application and referenced window was readable."""
        return not self.unavailable_buses and not self.unavailable_windows and not self.unavailable_sources
