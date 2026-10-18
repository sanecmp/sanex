"""Bounded optional metadata reads and snapshot cache behavior."""

import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from sanex.applications.common import MAX_METADATA_BYTES, ApplicationContext, MetadataReader, read_bounded_file
from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import ProcessReader, WindowProcessResolver


def test_keeps_only_whitelisted_environment_hints(
    process_create: Callable[..., ProcessIdentity], tmp_path: Path,
) -> None:
    process = process_create(environment={"APP_KEY": "123", "PASSWORD": "synthetic-secret"})
    context = ApplicationContext(ProcessReader(tmp_path).read, MetadataReader(tmp_path), environment_keys=frozenset({"APP_KEY"}))

    assert context.read_environment(process) == {"APP_KEY": "123"}


@pytest.mark.parametrize("kind", ["missing", "oversized", "symlink", "fifo", "directory"])
def test_rejects_unavailable_or_unsafe_metadata_without_blocking(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "metadata"

    if kind == "oversized":
        path.write_bytes(b"x" * (MAX_METADATA_BYTES + 1))

    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("synthetic secret")
        path.symlink_to(target)

    elif kind == "fifo":
        os.mkfifo(path)

    elif kind == "directory":
        path.mkdir()

    assert read_bounded_file(path) is None


@pytest.mark.parametrize("change", ["pid-reuse", "exec", "disappeared"])
def test_metadata_rechecks_process_identity(
    process_create: Callable[..., ProcessIdentity], tmp_path: Path, change: str,
) -> None:
    process = process_create(environment={"SteamAppId": "123"})

    if change == "pid-reuse":
        process_create(started=200, environment={"SteamAppId": "999"})

    elif change == "exec":
        process_create(exe="/usr/bin/replaced", environment={"SteamAppId": "999"})

    else:
        (tmp_path / "100" / "stat").unlink()

    assert MetadataReader(tmp_path).read(process, "environ") is None


def test_reads_identity_and_metadata_once_per_process_not_per_window(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_create(exe="/usr/bin/gjs-console", arguments=("gjs", "/usr/share/example/app.js"))
    identity_reads = []
    metadata_reads = []
    read_process = ProcessReader.read
    read_metadata = MetadataReader.read

    def count_process(reader: ProcessReader, pid: int) -> ProcessIdentity | None:
        identity_reads.append(pid)
        return read_process(reader, pid)

    def count_metadata(reader: MetadataReader, process: ProcessIdentity, member: str) -> bytes | None:
        metadata_reads.append((process.pid, member))
        return read_metadata(reader, process, member)

    monkeypatch.setattr(ProcessReader, "read", count_process)
    monkeypatch.setattr(MetadataReader, "read", count_metadata)
    windows = tuple(window_create(number=number) for number in range(100))

    groups = resolver.resolve(windows, 1001)

    assert len(groups) == 1
    assert len(groups[0].windows) == 100
    assert identity_reads == [100, 1]
    assert Counter(metadata_reads) == Counter({(100, "environ"): 1, (100, "cmdline"): 1})


def test_retries_metadata_next_snapshot_including_same_interpreter_exec(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(exe="/usr/bin/gjs-console", arguments=("gjs", "/apps/first.js"))
    first = resolver.resolve((window_create(),), 1001)[0]
    process_create(exe="/usr/bin/gjs-console", arguments=("gjs", "/apps/second.js"))
    second = resolver.resolve((window_create(),), 1001)[0]

    assert first.application.ident == "gjs:/apps/first.js"
    assert second.application.ident == "gjs:/apps/second.js"
    assert first.ident != second.ident


def test_foreign_uid_is_filtered_before_optional_metadata_reads(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_create(uid=2000)

    def fail_read(*args: object) -> None:
        pytest.fail("foreign process metadata must not be read")

    monkeypatch.setattr(MetadataReader, "read", fail_read)

    assert resolver.resolve((window_create(),), 1001) == ()
