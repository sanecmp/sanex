"""Desktop launcher hints and a bounded cache of installed display names."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from sanelib.protocol import ApplicationIdentity

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch, parse_desktop_id, parse_key_file, read_bounded_file


ENVIRONMENT_KEYS = frozenset({"GIO_LAUNCHED_DESKTOP_FILE", "GIO_LAUNCHED_DESKTOP_FILE_PID", "BAMF_DESKTOP_FILE_HINT"})


@dataclass(slots=True)
class DesktopCatalog:
    """Look up only requested desktop files, never scan application directories."""

    directories: tuple[Path, ...] = (
        Path("/usr/local/share/applications"), Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    )
    clock: Callable[[], float] = monotonic
    capacity: int = 256
    refresh_seconds: int = 300
    _names: OrderedDict[str, tuple[float, str]] = field(default_factory=OrderedDict, init=False, repr=False)

    def __post_init__(self) -> None:

        if self.capacity <= 0 or self.refresh_seconds <= 0:
            raise ValueError("desktop cache capacity and refresh interval must be positive")

    def find_name(self, desktop_id: str) -> str:
        """Return an installed Name or the ID; cache misses too, within a fixed bound."""

        if parse_desktop_id(desktop_id) is None:
            return desktop_id

        now = self.clock()
        cached = self._names.get(desktop_id)

        if cached is not None and now - cached[0] < self.refresh_seconds:
            self._names.move_to_end(desktop_id)
            return cached[1]

        name = desktop_id

        for directory in self.directories:
            try:
                target = (directory / f"{desktop_id}.desktop").resolve(strict=True)
                trusted_directory = directory.resolve()

            except (OSError, RuntimeError):
                continue

            # Flatpak's system exports are symlinks to its deployment directory.
            trusted = target.is_relative_to(trusted_directory) or (
                directory == Path("/var/lib/flatpak/exports/share/applications")
                and target.is_relative_to(Path("/var/lib/flatpak/app"))
            )

            if not trusted:
                continue

            value = parse_key_file(read_bounded_file(target), "Desktop Entry", "Name")

            if value:
                name = value
                break

        self._names[desktop_id] = (now, name)
        self._names.move_to_end(desktop_id)

        while len(self._names) > self.capacity:
            self._names.popitem(last=False)

        return name


def identify_desktop(process: ProcessIdentity, context: ApplicationContext, catalog: DesktopCatalog) -> ApplicationMatch | None:
    """Use a launcher's desktop-file basename, not arbitrary user-supplied paths."""
    environment = context.read_environment(process)
    launcher_pid = environment.get("GIO_LAUNCHED_DESKTOP_FILE_PID")
    filename = environment.get("GIO_LAUNCHED_DESKTOP_FILE")

    if launcher_pid is not None and launcher_pid != f"{process.pid}":
        filename = None

    filename = filename or environment.get("BAMF_DESKTOP_FILE_HINT")

    if not filename or not filename.endswith(".desktop"):
        return None

    desktop_id = parse_desktop_id(Path(filename).name.removesuffix(".desktop"))

    if desktop_id is None:
        return None

    return ApplicationMatch(process, ApplicationIdentity(ident=f"desktop:{desktop_id}", name=catalog.find_name(desktop_id)))
