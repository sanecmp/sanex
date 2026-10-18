"""Desktop IDs, safe lookup and bounded display-name caching."""

from collections.abc import Callable
from pathlib import Path

import pytest

from sanex.applications.desktop import DesktopCatalog
from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import WindowProcessResolver


def test_uses_desktop_id_instead_of_interpreter_name(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(exe="/usr/bin/gjs-console", environment={
        "GIO_LAUNCHED_DESKTOP_FILE": "/arbitrary/location/org.example.Maps.desktop", "GIO_LAUNCHED_DESKTOP_FILE_PID": "100",
    })

    group = resolver.resolve((window_create(),), 1001)[0]

    assert group.application.ident == "desktop:org.example.Maps"
    assert group.process.exe == "/usr/bin/gjs-console"


def test_ignores_inherited_gio_launcher_identity(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(environment={"GIO_LAUNCHED_DESKTOP_FILE": "/apps/Other.desktop", "GIO_LAUNCHED_DESKTOP_FILE_PID": "99"})

    assert resolver.resolve((window_create(),), 1001)[0].application is None


def test_name_cache_refreshes_negative_results_and_evicts_old_entries(tmp_path: Path) -> None:
    now = [100.0]
    catalog = DesktopCatalog((tmp_path,), clock=lambda: now[0], capacity=2, refresh_seconds=30)

    assert catalog.find_name("org.example.App") == "org.example.App"
    desktop = tmp_path / "org.example.App.desktop"
    desktop.write_text("[Desktop Entry]\nName=Synthetic editor\n")
    assert catalog.find_name("org.example.App") == "org.example.App"
    now[0] += 30
    assert catalog.find_name("org.example.App") == "Synthetic editor"
    desktop.write_text("[Desktop Entry]\nName=Renamed editor\n")
    catalog.find_name("org.example.Second")
    catalog.find_name("org.example.Third")
    assert catalog.find_name("org.example.App") == "Renamed editor"


@pytest.mark.parametrize("app_id", ["../../secret", "/absolute/secret", "bad\nname"])
def test_unsafe_desktop_id_is_not_used_as_a_path(tmp_path: Path, app_id: str) -> None:
    assert DesktopCatalog((tmp_path,)).find_name(app_id) == app_id


def test_reads_only_desktop_symlinks_within_the_configured_directory(tmp_path: Path) -> None:
    directory = tmp_path / "applications"
    directory.mkdir()
    target = directory / "real.desktop"
    target.write_text("[Desktop Entry]\nName=Synthetic editor\n")
    (directory / "org.example.Editor.desktop").symlink_to(target)
    outside = tmp_path / "private.desktop"
    outside.write_text("[Desktop Entry]\nName=Private value\n")
    (directory / "org.example.Outside.desktop").symlink_to(outside)
    catalog = DesktopCatalog((directory,))

    assert catalog.find_name("org.example.Editor") == "Synthetic editor"
    assert catalog.find_name("org.example.Outside") == "org.example.Outside"
