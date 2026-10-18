"""Flatpak descriptors, proxy attribution and safe fallback."""

from collections.abc import Callable
from pathlib import Path

import pytest

from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import WindowProcessResolver


@pytest.mark.parametrize("is_proxy", [True, False])
def test_identifies_flatpak_application_without_changing_observed_pid(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, is_proxy: bool,
) -> None:
    process = process_create(
        exe="/usr/bin/xdg-dbus-proxy" if is_proxy else "/app/bin/editor",
        name="xdg-dbus-proxy" if is_proxy else "editor",
        flatpak_id="org.example.PhotoCraft", environment={} if is_proxy else {"FLATPAK_ID": "org.example.PhotoCraft"},
    )

    group = resolver.resolve((window_create(),), 1001)[0]

    assert group.application.ident == "flatpak:org.example.PhotoCraft"
    assert group.process == process
    assert group.owners == (process,)


@pytest.mark.parametrize("descriptor", [b"invalid", b"[Application]\nname=../../secret\n", b"\xff"])
def test_invalid_descriptor_keeps_raw_proxy_identity(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, tmp_path: Path, descriptor: bytes,
) -> None:
    process = process_create(exe="/usr/bin/xdg-dbus-proxy", flatpak_id="org.example.Editor")
    (tmp_path / "100" / "root" / ".flatpak-info").write_bytes(descriptor)

    group = resolver.resolve((window_create(),), 1001)[0]

    assert group.application is None
    assert group.process == process


def test_same_flatpak_id_does_not_merge_separate_launches(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(exe="/usr/bin/xdg-dbus-proxy", flatpak_id="org.example.Editor")
    process_create(pid=200, exe="/usr/bin/xdg-dbus-proxy", flatpak_id="org.example.Editor")

    groups = resolver.resolve((window_create(), window_create(pid=200)), 1001)

    assert len(groups) == 2
    assert groups[0].application == groups[1].application
    assert groups[0].ident != groups[1].ident
