"""Synthetic procfs entries: never inspect the test runner's real processes."""

from collections.abc import Callable
from pathlib import Path

import pytest

from sanex.applications.desktop import DesktopCatalog
from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow, WindowRole
from sanex.platform.process import ProcessReader, WindowProcessResolver
from sanex.service.applications import create_application_resolver


@pytest.fixture
def process_create(tmp_path: Path) -> Callable[..., ProcessIdentity]:
    def create(
        pid: int = 100, parent_pid: int = 1, exe: str = "/usr/bin/example", name: str = "example",
        uid: int = 1001, started: int | None = None, environment: dict[str, str] | None = None,
        arguments: tuple[str, ...] = (), flatpak_id: str | None = None,
    ) -> ProcessIdentity:
        directory = tmp_path / f"{pid}"
        directory.mkdir(exist_ok=True)
        ticks = pid if started is None else started
        fields = ["S", f"{parent_pid}", *("0" for _ in range(17)), f"{ticks}"]
        (directory / "stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
        (directory / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (directory / "comm").write_text(f"{name}\n")
        (directory / "cgroup").write_text("0::/synthetic/shared-desktop.service\n")
        executable = directory / "exe"
        executable.unlink(missing_ok=True)
        executable.symlink_to(exe)
        (directory / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in (environment or {}).items()))
        (directory / "cmdline").write_bytes(b"\0".join(argument.encode() for argument in arguments))

        if flatpak_id is not None:
            root = directory / "root"
            root.mkdir(exist_ok=True)
            (root / ".flatpak-info").write_text(f"[Application]\nname={flatpak_id}\n")

        process = ProcessReader(tmp_path).read(pid)
        assert process is not None
        return process

    return create


@pytest.fixture
def window_create() -> Callable[..., AtspiWindow]:
    def create(pid: int = 100, number: int = 1) -> AtspiWindow:
        return AtspiWindow(bus=f":1.{pid}", path=f"/window/{number}", pid=pid, title="Synthetic window", role=WindowRole.FRAME)

    return create


@pytest.fixture
def resolver(tmp_path: Path) -> WindowProcessResolver:
    return WindowProcessResolver(
        ProcessReader(tmp_path),
        applications=create_application_resolver(tmp_path, catalog=DesktopCatalog(directories=())),
    )
