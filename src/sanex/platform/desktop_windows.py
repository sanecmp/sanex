"""Combine accessibility and X11 observations without counting windows twice."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sanelib.protocol import ObservationSource

from ..exceptions import AtspiError, X11Error
from ..model.window import AtspiWindow, AtspiWindowSnapshot
from ..utils.resources import close_resources
from .atspi import AtspiClient
from .x11 import X11Client


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DesktopWindowClient:
    """Keep AT-SPI primary and use EWMH for processes absent from accessibility."""

    accessibility: AtspiClient = field(default_factory=AtspiClient)
    x11: X11Client = field(default_factory=X11Client)
    _accessibility_ready: bool = field(default=False, init=False)
    _x11_unavailable: bool = field(default=False, init=False)

    async def connect(self, environment: Mapping[str, str] | None = None, home: Path | None = None) -> None:
        """Open both sources explicitly while running as the session user."""

        try:
            await self.accessibility.connect()
            self._accessibility_ready = True

        except AtspiError:
            logger.warning("Accessibility source is unavailable; X11 observation remains available")

        try:
            self.x11.connect(os.environ if environment is None else environment, Path.home() if home is None else home)

        except X11Error:
            logger.warning("X11 source is unavailable; using accessibility only")
            self._x11_unavailable = True

    def close(self) -> None:
        """Release both sources even if one close operation fails."""
        close_resources(("close X11 client", self.x11.close), ("close accessibility client", self.accessibility.close))

    async def read_windows(self) -> AtspiWindowSnapshot:
        """Merge snapshots, preserving runs belonging to an inaccessible source."""

        try:

            if not self._accessibility_ready:
                await self.accessibility.connect()
                self._accessibility_ready = True

            accessible = await self.accessibility.read_windows()

        except AtspiError:
            accessible = AtspiWindowSnapshot(unavailable_sources=(ObservationSource.ATSPI,))

        try:
            supplemental = self.x11.read_windows()

        except X11Error:
            supplemental = AtspiWindowSnapshot(unavailable_sources=(ObservationSource.EWMH,))

        accessible_pids = {window.pid for window in accessible.windows}
        return AtspiWindowSnapshot(
            windows=(*accessible.windows, *(window for window in supplemental.windows if window.pid not in accessible_pids)),
            unavailable_buses=(*accessible.unavailable_buses, *supplemental.unavailable_buses),
            unavailable_windows=(*accessible.unavailable_windows, *supplemental.unavailable_windows),
            unavailable_sources=(
                *accessible.unavailable_sources, *supplemental.unavailable_sources,
                *((ObservationSource.EWMH,) if self._x11_unavailable else ()),
            ),
        )

    async def close_window(self, window: AtspiWindow) -> bool:
        """Route a close action to the source that supplied the window."""

        if window.source == ObservationSource.EWMH:
            return self.x11.close_window(window)

        return await self.accessibility.close_window(window)
