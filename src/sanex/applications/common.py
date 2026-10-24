"""Shared bounded metadata reads and snapshot-local application context."""

import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass, field
from pathlib import Path

from sanelib.protocol import ApplicationIdentity

from ..exceptions import ProcessError
from ..model.process import ProcessIdentity


MAX_METADATA_BYTES = 65_536
MAX_ANCESTORS = 32
_DESKTOP_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}")
logger = logging.getLogger(__name__)


def read_bounded_file(path: Path) -> bytes | None:
    """Read a small regular file without following its final symlink or blocking on a FIFO."""
    descriptor = None

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)

        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None

        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(MAX_METADATA_BYTES + 1)

        return data if len(data) <= MAX_METADATA_BYTES else None

    except OSError as error:
        # Metadata is optional; never log command lines, environment values or file contents.
        logger.debug("Application metadata unavailable (%s)", type(error).__name__)
        return None

    finally:

        if descriptor is not None:
            os.close(descriptor)


def parse_key_file(data: bytes | None, section: str, key: str) -> str | None:
    """Read one bounded INI key without interpolation."""

    if data is None:
        return None

    parser = ConfigParser(interpolation=None, strict=False)

    try:
        parser.read_string(data.decode("utf-8"))
        value = parser.get(section, key, fallback="").strip()

    except (ConfigError, UnicodeError, ValueError):
        return None

    return value if value and len(value) <= 512 and not any(ord(char) < 32 for char in value) else None


def parse_desktop_id(value: str) -> str | None:
    """Accept a desktop identifier, never a relative filesystem path."""
    return value if _DESKTOP_ID.fullmatch(value) and ".." not in value else None


@dataclass(frozen=True, slots=True)
class ApplicationMatch:
    """Semantic identity and the process anchoring this particular launch."""

    process: ProcessIdentity
    application: ApplicationIdentity | None = None
    group_same_executable: bool = True
    fallback_name: str | None = None

    @property
    def app_id(self) -> str | None:
        """Return the recognition key, independent of display-name changes."""
        return self.application.ident if self.application else None


@dataclass(frozen=True, slots=True)
class MetadataReader:
    """Read optional data only for explicitly requested procfs identities."""

    proc_root: Path = Path("/proc")

    def read(self, process: ProcessIdentity, member: str) -> bytes | None:
        """Read metadata and reject a vanished, reused or exec-replaced process."""
        directory = self.proc_root / f"{process.pid}"
        data = read_bounded_file(directory / member)

        if data is None:
            return None

        try:
            # Verify after the optional read, independently of the snapshot PID cache.
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()

            if int(fields[19]) != process.started or os.readlink(directory / "exe") != process.exe:
                return None

        except (OSError, UnicodeError, ValueError, IndexError):
            return None

        return data


@dataclass(slots=True)
class ApplicationContext:
    """Lazy, shared caches discarded after each window snapshot, including failures."""

    read_process: Callable[[int], ProcessIdentity | None]
    metadata: MetadataReader
    environment_keys: frozenset[str] = frozenset()
    environments: dict[ProcessIdentity, dict[str, str]] = field(default_factory=dict)
    arguments: dict[ProcessIdentity, tuple[str, ...]] = field(default_factory=dict)
    files: dict[tuple[ProcessIdentity, str], bytes | None] = field(default_factory=dict)
    matches: dict[ProcessIdentity, ApplicationMatch] = field(default_factory=dict)
    window_titles: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def read_environment(self, process: ProcessIdentity) -> dict[str, str]:
        """Keep only recognition hints; do not retain arbitrary environment secrets."""

        if process not in self.environments:
            data = (self.metadata.read(process, "environ") or b"") if self.environment_keys else b""
            values = {}

            for entry in data.split(b"\0"):
                key, separator, value = entry.partition(b"=")
                name = key.decode("utf-8", errors="replace")

                if separator and name in self.environment_keys:
                    values[name] = value.decode("utf-8", errors="replace")

            self.environments[process] = values

        return self.environments[process]

    def read_arguments(self, process: ProcessIdentity) -> tuple[str, ...]:
        """Read arguments lazily for an interpreter, not for every observed process."""

        if process not in self.arguments:
            data = self.metadata.read(process, "cmdline") or b""
            self.arguments[process] = tuple(
                item.decode("utf-8", errors="replace") for item in data.rstrip(b"\0").split(b"\0") if item
            )

        return self.arguments[process]

    def read_file(self, process: ProcessIdentity, member: str) -> bytes | None:
        """Share one optional file read between providers in this snapshot."""
        key = (process, member)

        if key not in self.files:
            self.files[key] = self.metadata.read(process, member)

        return self.files[key]

    def walk_parents(self, process: ProcessIdentity) -> Iterator[ProcessIdentity]:
        """Follow a bounded same-UID chain, tolerating disappearance and cycles."""
        visited = {process.pid}
        current = process

        for _ in range(MAX_ANCESTORS):

            if current.parent_pid == 0 or current.parent_pid in visited:
                return

            visited.add(current.parent_pid)

            try:
                parent = self.read_process(current.parent_pid)

            except ProcessError:
                return

            if parent is None or parent.uid != process.uid:
                return

            yield parent
            current = parent


ApplicationIdentifier = Callable[[ProcessIdentity, ApplicationContext], ApplicationMatch | None]


@dataclass(frozen=True, slots=True)
class ApplicationProvider:
    """Recognition callback and the environment keys it needs, if any."""

    identify: ApplicationIdentifier
    environment_keys: frozenset[str] = frozenset()
