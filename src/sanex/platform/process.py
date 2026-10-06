"""Linux /proc process identity and generic AT-SPI window grouping."""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import ValidationError

from ..exceptions import ProcessError
from ..model.process import ProcessIdentity
from ..model.window import AtspiWindow


@dataclass(frozen=True, slots=True)
class ProcessReader:
    """Read stable process identity fields from procfs."""

    proc_root: Path = Path("/proc")

    def read(self, pid: int) -> ProcessIdentity | None:
        """Return one identity, or ``None`` if the process vanished or was reused."""

        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ProcessError(pid, ValueError("PID is invalid"))

        directory = self.proc_root / f"{pid}"
        try:
            parent_pid, started = self._read_stat(directory / "stat", pid)
            prc_name = (directory / "comm").read_text().removesuffix("\n")
            exe = os.readlink(directory / "exe")
            uid = self._read_uid(directory / "status")
            cgroup = self._read_cgroup(directory / "cgroup")

            if self._read_stat(directory / "stat", pid)[1] != started:
                return None

            return ProcessIdentity(
                pid=pid,
                parent_pid=parent_pid,
                uid=uid,
                started=started,
                prc_name=prc_name,
                exe=exe,
                cgroup=cgroup,
            )

        except FileNotFoundError:
            return None

        except (OSError, UnicodeError, ValueError, ValidationError) as error:
            raise ProcessError(pid, error) from error

    @staticmethod
    def _read_stat(path: Path, pid: int) -> tuple[int, int]:
        data = path.read_text().strip()
        prefix = f"{pid} ("
        closing = data.rfind(")")

        if not data.startswith(prefix) or closing < len(prefix):
            raise ValueError("stat has an invalid process header")

        fields = data[closing + 1 :].split()

        if len(fields) <= 19:
            raise ValueError("stat does not contain process start time")

        parent_pid = int(fields[1])
        started = int(fields[19])

        if parent_pid < 0 or started < 0:
            raise ValueError("stat contains a negative process identity field")

        return parent_pid, started

    @staticmethod
    def _read_uid(path: Path) -> int:
        uid_line = next(
            (line for line in path.read_text().splitlines() if line.startswith("Uid:")),
            None,
        )

        if uid_line is None:
            raise ValueError("status does not contain Uid")

        fields = uid_line.split()

        if len(fields) != 5:
            raise ValueError("status contains an invalid Uid")

        uid = int(fields[1])

        if uid < 0:
            raise ValueError("status contains a negative Uid")

        return uid

    @staticmethod
    def _read_cgroup(path: Path) -> str | None:
        fallback = None

        for line in path.read_text().splitlines():
            fields = line.split(":", 2)

            if len(fields) != 3 or not fields[2].startswith("/"):
                raise ValueError("cgroup contains an invalid entry")

            controllers = fields[1].split(",") if fields[1] else []

            if not controllers:
                return fields[2]

            if "name=systemd" in controllers:
                fallback = fields[2]

        return fallback


class ProcessSource(Protocol):
    """Source of stable Linux process identities."""

    def read(self, pid: int) -> ProcessIdentity | None: ...


@dataclass(frozen=True, slots=True)
class ProcessWindowGroup:
    """Top-level windows belonging to one stable application process."""

    process: ProcessIdentity
    windows: tuple[AtspiWindow, ...]
    owners: tuple[ProcessIdentity, ...]

    @property
    def ident(self) -> str:
        """Return a stable group identity for the lifetime of the process tree."""
        process = self.process
        return f"{process.pid}:{process.started}"

    def find_owner(self, window: AtspiWindow) -> ProcessIdentity:
        """Return the concrete process that owns one grouped window."""

        for owner in self.owners:

            if owner.pid == window.pid:
                return owner

        raise ProcessError(window.pid, ValueError("window owner is absent from group"))


@dataclass(frozen=True, slots=True)
class WindowProcessResolver:
    """Resolve window owners and group same-executable process trees."""

    reader: ProcessSource

    def resolve(
        self,
        windows: Iterable[AtspiWindow],
        uid: int,
        ignored_prcs: Iterable[str] = (),
    ) -> tuple[ProcessWindowGroup, ...]:
        """Return same-UID windows grouped by a stable application root.

        AT-SPI supplies only top-level window owners. When several owners are
        descendants with the same executable, they belong to one application
        run. This groups browser-style workers without knowing any application
        names and keeps separately launched process trees apart. Cgroups are
        not grouping keys because one application can span several scopes.
        """
        ignored = frozenset(ignored_prcs)
        identities: dict[int, ProcessIdentity | None] = {}
        roots: dict[tuple[int, int], ProcessIdentity] = {}
        grouped: dict[
            tuple[int, int],
            list[tuple[AtspiWindow, ProcessIdentity]],
        ] = {}

        for window in windows:
            owner = self._read_process(window.pid, identities)

            if owner is None:
                continue

            process = self._find_application_root(owner, identities)

            if (
                process.uid != uid
                or process.prc_name in ignored
                or self._is_system_component(process)
            ):
                continue

            key = (process.pid, process.started)
            roots[key] = process
            grouped.setdefault(key, []).append((window, owner))

        groups = []

        for key in sorted(grouped):
            members = sorted(
                grouped[key],
                key=lambda item: (item[0].bus, item[0].path),
            )
            groups.append(
                ProcessWindowGroup(
                    process=roots[key],
                    windows=tuple(window for window, owner in members),
                    owners=tuple(owner for window, owner in members),
                )
            )

        return tuple(groups)

    def _read_process(
        self,
        pid: int,
        identities: dict[int, ProcessIdentity | None],
    ) -> ProcessIdentity | None:

        if pid not in identities:
            identities[pid] = self.reader.read(pid)

        return identities[pid]

    def _find_application_root(
        self,
        process: ProcessIdentity,
        identities: dict[int, ProcessIdentity | None],
    ) -> ProcessIdentity:
        current = process
        visited = {current.pid}

        while current.parent_pid != 0:
            parent_pid = current.parent_pid

            if parent_pid in visited:
                break

            visited.add(parent_pid)
            try:
                parent = self._read_process(parent_pid, identities)

            except ProcessError:
                break

            if (
                parent is None
                or parent.uid != current.uid
                or parent.exe != current.exe
            ):
                break

            current = parent

        return current

    @staticmethod
    def _is_system_component(process: ProcessIdentity) -> bool:
        cgroup = process.cgroup

        if cgroup is None:
            return False

        components = set(PurePosixPath(cgroup).parts)
        return bool({"session.slice", "background.slice"} & components)
