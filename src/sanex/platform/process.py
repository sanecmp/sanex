"""Linux /proc process identity and generic AT-SPI window grouping."""

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError
from sanelib.protocol import ApplicationIdentity, ObservationSource

from ..applications import ApplicationResolver
from ..applications.common import ApplicationContext, ApplicationMatch
from ..exceptions import ProcessError
from ..model.process import ProcessIdentity
from ..model.window import AtspiWindow


@dataclass(frozen=True, slots=True)
class ProcessReader:
    """Read stable process identity fields from procfs."""

    proc_root: Path = Path("/proc")

    def list_pids(self) -> tuple[int, ...]:
        """Enumerate PIDs only when an explicitly enabled observer requests it."""

        try:
            return tuple(sorted(int(entry.name) for entry in self.proc_root.iterdir() if entry.name.isascii() and entry.name.isdecimal()))

        except OSError as failure:
            raise ProcessError(0, failure) from failure

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


class ProcessObserver(Protocol):
    """Optional application-specific discovery sharing the window snapshot cache."""

    def observe(
        self, uid: int, sess_ident: str, context: ApplicationContext, matches: tuple[ApplicationMatch, ...],
    ) -> tuple[ApplicationMatch, ...]: ...

    def retain_sessions(self, uid: int, sess_idents: frozenset[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class ProcessWindowGroup:
    """Top-level windows belonging to one stable application process."""

    process: ProcessIdentity
    windows: tuple[AtspiWindow, ...]
    owners: tuple[ProcessIdentity, ...]
    application: ApplicationIdentity | None = None

    @property
    def source(self) -> ObservationSource:
        """Use the window source, or process lifetime for a windowless launch."""
        return self.windows[0].source if self.windows else ObservationSource.PROCESS

    @property
    def ident(self) -> str:
        """Return a stable group identity for the lifetime of the process tree."""
        process = self.process
        identity = f"{process.pid}:{process.started}"
        return identity if self.application is None else f"{identity}:{self.application.ident}"

    def find_owner(self, window: AtspiWindow) -> ProcessIdentity:
        """Return the concrete process that owns one grouped window."""

        for owner in self.owners:

            if owner.pid == window.pid:
                return owner

        raise ProcessError(window.pid, ValueError("window owner is absent from group"))


@dataclass(frozen=True, slots=True)
class WindowProcessResolver:
    """Resolve window owners using an injected, application-agnostic pipeline."""

    reader: ProcessSource
    applications: ApplicationResolver = field(default_factory=ApplicationResolver)
    observer: ProcessObserver | None = None

    def retain_sessions(self, uid: int, sess_idents: frozenset[str]) -> None:
        """Discard optional discovery state when accounts or sessions stop being observed."""

        if self.observer is not None:
            self.observer.retain_sessions(uid, sess_idents)

    def resolve(
        self,
        windows: Iterable[AtspiWindow],
        uid: int,
        ignored_prcs: Iterable[str] = (),
        *,
        sess_ident: str = "",
    ) -> tuple[ProcessWindowGroup, ...]:
        """Return same-UID windows grouped by a stable launch root.

        Without configured providers, only same-executable descendants group.
        Providers can recognize applications and select their launch roots;
        a shared application ID never merges independent process trees.
        Cgroups are not grouping keys or filters: an application can inherit
        its launcher's desktop service or span several scopes.
        """
        # GNOME Shell exposes the desktop itself as an AT-SPI window. Ignore
        # the shell's process, not other applications inheriting its cgroup.
        ignored = frozenset(("gnome-shell", *ignored_prcs))
        windows = tuple(windows)
        identities: dict[int, ProcessIdentity | None] = {}
        context = ApplicationContext(
            read_process=lambda pid: self._read_process(pid, identities),
            metadata=self.applications.metadata,
            environment_keys=self.applications.environment_keys,
            window_titles={pid: tuple(window.title for window in windows if window.pid == pid) for pid in {window.pid for window in windows}},
        )
        resolved: dict[ProcessIdentity, ApplicationMatch] = {}
        roots: dict[tuple[int, int, str], ApplicationMatch] = {}
        grouped: dict[
            tuple[int, int, str],
            list[tuple[AtspiWindow, ProcessIdentity]],
        ] = {}

        for window in windows:
            owner = self._read_process(window.pid, identities)

            if owner is None or owner.uid != uid:
                continue

            if owner not in resolved:
                resolved[owner] = self.applications.resolve(owner, context)

            match = resolved[owner]
            process = match.process

            if process.uid != uid or process.prc_name in ignored:
                continue

            key = (process.pid, process.started, match.application.ident if match.application else "")
            roots[key] = match
            grouped.setdefault(key, []).append((window, owner))

        if self.observer is not None and sess_ident:

            for match in self.observer.observe(uid, sess_ident, context, tuple(resolved.values())):
                process = match.process

                if process.uid != uid or process.prc_name in ignored:
                    continue

                key = (process.pid, process.started, match.application.ident if match.application else "")
                roots.setdefault(key, match)
                grouped.setdefault(key, [])

        groups = []

        for key in sorted(grouped):
            members = sorted(
                grouped[key],
                key=lambda item: (item[0].bus, item[0].path),
            )
            groups.append(
                ProcessWindowGroup(
                    process=roots[key].process,
                    application=roots[key].application,
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
