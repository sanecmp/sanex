"""AppImage identities must not depend on temporary mount directories."""

from collections.abc import Callable
from pathlib import Path

import pytest

from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import WindowProcessResolver


IMAGE = "/home/example/Applications/BambuStudio.AppImage"
MOUNT = "/tmp/.mount_BambuS123456"
EXECUTABLE = f"{MOUNT}/usr/bin/bambustu_main"


def test_appimage_launches_share_an_identity_across_temporary_mounts(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    first = process_create(exe=EXECUTABLE, name="bambustu_main", environment={"APPIMAGE": IMAGE, "APPDIR": MOUNT})
    second_mount = "/tmp/.mount_BambuS654321"
    second = process_create(
        pid=200, exe=f"{second_mount}/usr/bin/bambustu_main", name="bambustu_main",
        environment={"APPIMAGE": IMAGE, "APPDIR": second_mount},
    )

    groups = resolver.resolve((window_create(), window_create(pid=200)), 1001)

    assert [group.application.ident for group in groups] == [f"appimage:{IMAGE}"] * 2
    assert [group.application.name for group in groups] == ["BambuStudio"] * 2
    assert [group.process for group in groups] == [first, second]
    assert groups[0].ident != groups[1].ident


def test_appimages_with_the_same_executable_name_remain_distinct(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(exe=EXECUTABLE, environment={"APPIMAGE": IMAGE, "APPDIR": MOUNT})
    other_image = "/home/example/Other/BambuStudio.AppImage"
    process_create(pid=200, exe=EXECUTABLE, environment={"APPIMAGE": other_image, "APPDIR": MOUNT})

    groups = resolver.resolve((window_create(), window_create(pid=200)), 1001)

    assert [group.application.ident for group in groups] == [f"appimage:{IMAGE}", f"appimage:{other_image}"]


def test_appimage_workers_group_but_inherited_hints_do_not_claim_external_programs(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    environment = {"APPIMAGE": IMAGE, "APPDIR": MOUNT}
    parent = process_create(exe=EXECUTABLE, environment=environment)
    process_create(pid=101, parent_pid=100, exe=EXECUTABLE, environment=environment)
    external = process_create(
        pid=200, parent_pid=100, exe=f"{MOUNT}-other/usr/bin/bambustu_main", environment=environment,
    )

    groups = resolver.resolve((window_create(), window_create(pid=101), window_create(pid=200)), 1001)

    assert [group.process for group in groups] == [parent, external]
    assert len(groups[0].windows) == 2
    assert groups[0].application.ident == f"appimage:{IMAGE}"
    assert groups[1].application is None


@pytest.mark.parametrize("environment", [
    {},
    {"APPIMAGE": IMAGE},
    {"APPDIR": MOUNT},
    {"APPIMAGE": "relative.AppImage", "APPDIR": MOUNT},
    {"APPIMAGE": IMAGE, "APPDIR": "relative"},
    {"APPIMAGE": IMAGE, "APPDIR": "/"},
    {"APPIMAGE": "/home/example/../Applications/BambuStudio.AppImage", "APPDIR": MOUNT},
    {"APPIMAGE": IMAGE, "APPDIR": f"{MOUNT}/../"},
    {"APPIMAGE": f"{IMAGE}\n", "APPDIR": MOUNT},
    {"APPIMAGE": IMAGE, "APPDIR": f"{MOUNT}\n"},
    {"APPIMAGE": f"{MOUNT}/nested.AppImage", "APPDIR": MOUNT},
    {"APPIMAGE": f"/{'x' * 4000}", "APPDIR": MOUNT},
])
def test_appimage_does_not_guess_identity_from_missing_or_invalid_metadata(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, environment: dict[str, str],
) -> None:
    process = process_create(exe=EXECUTABLE, environment=environment)

    group = resolver.resolve((window_create(),), 1001)[0]

    assert group.application is None
    assert group.process == process


def test_appimage_without_readable_environment_preserves_raw_process_identity(
    tmp_path: Path, process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver,
) -> None:
    process = process_create(exe=EXECUTABLE, environment={"APPIMAGE": IMAGE, "APPDIR": MOUNT})
    (tmp_path / "100" / "environ").unlink()

    group = resolver.resolve((window_create(),), 1001)[0]

    assert group.application is None
    assert group.process == process


def test_appimage_identity_is_independent_of_desktop_launcher_hints(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(exe=EXECUTABLE, environment={
        "APPIMAGE": IMAGE, "APPDIR": MOUNT,
        "GIO_LAUNCHED_DESKTOP_FILE": "/arbitrary/launcher.desktop", "GIO_LAUNCHED_DESKTOP_FILE_PID": "100",
    })

    group = resolver.resolve((window_create(),), 1001)[0]

    assert group.application.ident == f"appimage:{IMAGE}"
