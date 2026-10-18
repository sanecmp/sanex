"""Provider injection without built-in application knowledge."""

from collections.abc import Callable
from pathlib import Path

import pytest
from sanelib.protocol import ApplicationIdentity

from sanex.applications import ApplicationResolver
from sanex.applications.common import ApplicationContext, ApplicationMatch, ApplicationProvider, MetadataReader
from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import ProcessReader, WindowProcessResolver


def test_accepts_independent_providers_and_shares_their_declared_environment_keys(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = process_create(environment={"CUSTOM_APP": "editor", "SECRET": "not-retained"})
    calls = []
    reads = []
    read_metadata = MetadataReader.read

    def first(candidate: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch | None:
        calls.append("first")
        assert context.read_environment(candidate) == {"CUSTOM_APP": "editor"}
        return None

    def second(candidate: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch | None:
        calls.append("second")
        value = context.read_environment(candidate)["CUSTOM_APP"]
        return ApplicationMatch(candidate, ApplicationIdentity(ident=f"custom:{value}", name="Custom editor"))

    def third(candidate: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch | None:
        pytest.fail("must stop at the first recognized application")

    def count_reads(reader: MetadataReader, candidate: ProcessIdentity, member: str) -> bytes | None:
        reads.append(member)
        return read_metadata(reader, candidate, member)

    monkeypatch.setattr(MetadataReader, "read", count_reads)
    resolver = WindowProcessResolver(ProcessReader(tmp_path), ApplicationResolver(
        metadata=MetadataReader(tmp_path),
        providers=(
            ApplicationProvider(first, frozenset({"CUSTOM_APP"})),
            ApplicationProvider(second, frozenset({"CUSTOM_APP"})),
            ApplicationProvider(third),
        ),
    ))

    group = resolver.resolve((window_create(), window_create(number=2)), 1001)[0]

    assert group.process == process
    assert group.application.ident == "custom:editor"
    assert calls == ["first", "second"]
    assert reads == ["environ"]


def test_unconfigured_resolver_only_groups_processes_without_reading_optional_metadata(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_create()
    process_create(pid=101, parent_pid=100)

    def fail_read(*args: object) -> None:
        pytest.fail("no providers require optional metadata")

    monkeypatch.setattr(MetadataReader, "read", fail_read)
    resolver = WindowProcessResolver(ProcessReader(tmp_path), ApplicationResolver(metadata=MetadataReader(tmp_path)))

    groups = resolver.resolve((window_create(), window_create(pid=101)), 1001)

    assert len(groups) == 1
    assert groups[0].process.pid == 100
    assert groups[0].application is None
