"""Tests for procfs identity reading and generic window grouping."""

from pathlib import Path
from typing import Any

import pytest

from sanex.exceptions import ProcessError
from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow, WindowRole
from sanex.platform.process import ProcessReader, ProcessWindowGroup, WindowProcessResolver


def write_process(proc_root: Path, sample: dict[str, Any]) -> None:
    """Create a minimal procfs-like process directory from fixture data."""
    directory = proc_root / f"{sample["pid"]}"
    directory.mkdir(parents=True)
    (directory / "stat").write_text(sample["stat"])
    (directory / "status").write_text(sample["status"])
    (directory / "comm").write_text(f"{sample["prc_name"]}\n")
    (directory / "exe").symlink_to(sample["exe"])
    (directory / "cgroup").write_text(f"0::{sample["cgroup"]}\n")


def process_identity(process_payload: dict[str, Any], **changes: object) -> ProcessIdentity:
    """Build a validated identity from shared fixture data."""
    values = {
        key: process_payload[key]
        for key in (
            "pid",
            "parent_pid",
            "uid",
            "started",
            "prc_name",
            "exe",
            "cgroup",
        )
    }
    values.update(changes)
    return ProcessIdentity.model_validate(values)


def atspi_window(atspi_payload: dict[str, Any], **changes: object) -> AtspiWindow:
    """Build a validated window from shared fixture data."""
    sample = atspi_payload["children"][0]
    values = {
        "bus": sample["bus"],
        "path": sample["path"],
        "pid": sample["pid"],
        "title": sample["title"],
        "role": WindowRole(sample["role"]),
    }
    values.update(changes)
    return AtspiWindow.model_validate(values)


def test_reads_stable_process_identity(
    tmp_path: Path,
    process_payload: dict[str, Any],
) -> None:
    write_process(tmp_path, process_payload)

    process = ProcessReader(tmp_path).read(process_payload["pid"])

    assert process == process_identity(process_payload)


def test_vanished_process_is_not_an_error(tmp_path: Path) -> None:
    assert ProcessReader(tmp_path).read(5511) is None


def test_malformed_procfs_data_uses_application_exception(
    tmp_path: Path,
    process_payload: dict[str, Any],
) -> None:
    write_process(tmp_path, process_payload)
    (tmp_path / f"{process_payload["pid"]}" / "stat").write_text("broken")

    with pytest.raises(ProcessError, match="stat has an invalid process header"):
        ProcessReader(tmp_path).read(process_payload["pid"])


def test_groups_stable_process_across_atspi_buses_and_filters_other_uid(
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
) -> None:
    process = process_identity(process_payload)
    foreign = process_identity(process_payload, pid=6000, uid=1002, started=8_000_000)

    class FakeReader:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def read(self, pid: int) -> ProcessIdentity | None:
            self.calls.append(pid)
            return {process.pid: process, foreign.pid: foreign}.get(pid)

    reader = FakeReader()
    windows = (
        atspi_window(atspi_payload),
        atspi_window(atspi_payload, path="/org/a11y/atspi/accessible/2"),
        atspi_window(atspi_payload, bus=":1.21", path="/org/a11y/atspi/accessible/3"),
        atspi_window(
            atspi_payload,
            bus=":1.22",
            path="/org/a11y/atspi/accessible/4",
            pid=foreign.pid,
        ),
    )

    groups = WindowProcessResolver(reader=reader).resolve(windows, uid=process.uid)

    assert len(groups) == 1
    assert [(window.bus, window.path) for window in groups[0].windows] == [
        (":1.20", "/org/a11y/atspi/accessible/1"),
        (":1.20", "/org/a11y/atspi/accessible/2"),
        (":1.21", "/org/a11y/atspi/accessible/3"),
    ]
    assert reader.calls == [process.pid, 1, foreign.pid]
    assert all(group.process == process for group in groups)


def test_groups_same_executable_descendants_as_one_application_run(
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
) -> None:
    root = process_identity(process_payload)
    child = process_identity(
        process_payload,
        pid=6000,
        parent_pid=root.pid,
        started=root.started + 10,
        cgroup=f"{root.cgroup}-worker",
    )

    class FakeReader:
        def read(self, pid: int) -> ProcessIdentity | None:
            return {root.pid: root, child.pid: child}.get(pid)

    windows = (
        atspi_window(atspi_payload),
        atspi_window(
            atspi_payload,
            path="/org/a11y/atspi/accessible/2",
            pid=child.pid,
        ),
    )

    groups = WindowProcessResolver(FakeReader()).resolve(windows, uid=root.uid)

    assert len(groups) == 1
    group = groups[0]
    assert group.process == root
    assert group.owners == (root, child)
    assert group.find_owner(windows[0]) == root
    assert group.find_owner(windows[1]) == child


def test_group_requires_explicit_window_owner(
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
) -> None:
    process = process_identity(process_payload)
    window = atspi_window(atspi_payload)
    group = ProcessWindowGroup(process=process, windows=(window,), owners=())

    with pytest.raises(ProcessError, match="window owner is absent from group"):
        group.find_owner(window)


def test_keeps_different_executables_as_separate_application_runs(
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
) -> None:
    root = process_identity(process_payload)
    child = process_identity(
        process_payload,
        pid=6000,
        parent_pid=root.pid,
        started=root.started + 10,
        prc_name="helper",
        exe="/usr/lib/example/helper",
    )

    class FakeReader:
        def read(self, pid: int) -> ProcessIdentity | None:
            return {root.pid: root, child.pid: child}.get(pid)

    windows = (
        atspi_window(atspi_payload),
        atspi_window(
            atspi_payload,
            path="/org/a11y/atspi/accessible/2",
            pid=child.pid,
        ),
    )

    groups = WindowProcessResolver(FakeReader()).resolve(windows, uid=root.uid)

    assert [group.process for group in groups] == [root, child]


def test_keeps_readable_window_when_parent_cannot_be_inspected(
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
) -> None:
    process = process_identity(process_payload)

    class FakeReader:
        def read(self, pid: int) -> ProcessIdentity | None:

            if pid == process.pid:
                return process

            raise ProcessError(pid, PermissionError("denied"))

    groups = WindowProcessResolver(FakeReader()).resolve(
        (atspi_window(atspi_payload),),
        uid=process.uid,
    )

    assert len(groups) == 1
    assert groups[0].process == process


@pytest.mark.parametrize("prc_name", ["Telegram", "example_editor"])
@pytest.mark.parametrize(
    "cgroup_suffix",
    [
        "session.slice/dbus.service",
        "session.slice/dbus-broker.service",
        "session.slice/org.gnome.Shell@x11.service",
        "background.slice/launcher.service",
    ],
)
def test_keeps_application_windows_inherited_from_desktop_services(
    tmp_path: Path,
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
    prc_name: str,
    cgroup_suffix: str,
) -> None:
    sample = dict(process_payload)
    sample.update(
        uid=1000,
        prc_name=prc_name,
        exe=f"/home/example/soft/{prc_name}/{prc_name}",
        cgroup=f"/user.slice/user-1000.slice/user@1000.service/{cgroup_suffix}",
        stat=sample["stat"].replace(f"({sample["prc_name"]})", f"({prc_name})"),
        status=f"Name:\t{prc_name}\nUid:\t1000\t1000\t1000\t1000\n",
    )
    write_process(tmp_path, sample)
    window = atspi_window(atspi_payload)

    groups = WindowProcessResolver(ProcessReader(tmp_path)).resolve((window,), uid=1000)

    assert len(groups) == 1
    assert groups[0].process == process_identity(sample)
    assert groups[0].windows == (window,)


@pytest.mark.parametrize(
    ("changes", "ignored_prcs"),
    [
        ({"prc_name": "nautilus"}, ("nautilus",)),
        (
            {
                "prc_name": "gnome-shell",
                "exe": "/usr/bin/gnome-shell",
                "cgroup": (
                    "/user.slice/user-1001.slice/user@1001.service/"
                    "session.slice/org.gnome.Shell@x11.service"
                )
            },
            (),
        ),
        (
            {
                "prc_name": "gnome-shell",
                "exe": "/usr/bin/gnome-shell",
                "cgroup": None,
            },
            (),
        ),
        (
            {
                "prc_name": "Telegram",
                "cgroup": "/user.slice/user-1001.slice/user@1001.service/session.slice/dbus.service",
            },
            ("Telegram",),
        ),
    ],
)
def test_filters_configured_processes_and_the_desktop_shell(
    atspi_payload: dict[str, Any],
    process_payload: dict[str, Any],
    changes: dict[str, object],
    ignored_prcs: tuple[str, ...],
) -> None:
    process = process_identity(process_payload, **changes)

    class FakeReader:
        def read(self, pid: int) -> ProcessIdentity | None:
            return process if pid == process.pid else None

    groups = WindowProcessResolver(FakeReader()).resolve(
        (atspi_window(atspi_payload),),
        uid=process.uid,
        ignored_prcs=ignored_prcs,
    )

    assert groups == ()
