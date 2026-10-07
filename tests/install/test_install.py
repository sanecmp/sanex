"""Tests for sanex installation assets."""

import json
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[2]
INSTALLER = PROJECT_ROOT / "install.sh"
UNIT = PROJECT_ROOT / "systemd" / "sanex.service"
PATH_UNIT = PROJECT_ROOT / "systemd" / "sanex.path"
LOGROTATE = PROJECT_ROOT / "systemd" / "sanex.logrotate"
ICON = PROJECT_ROOT / "icons/hicolor/scalable/status/sanex-symbolic.svg"
WARNING_ICON = PROJECT_ROOT / "icons/hicolor/scalable/status/sanex-warning-symbolic.svg"
AUTOSTART = PROJECT_ROOT / "xdg/sanex-indicator.desktop"


def test_installer_has_valid_shell_syntax_and_help() -> None:
    subprocess.run(["sh", "-n", f"{INSTALLER}"], check=True)

    result = subprocess.run(
        [f"{INSTALLER}", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--uv PATH" in result.stdout
    assert "--python PATH" in result.stdout
    assert "--index-url URL" in result.stdout
    assert "--destdir DIRECTORY" in result.stdout
    assert "PACKAGE defaults to \"sanecmp-sanex\"" in result.stdout
    assert "python_command=/usr/bin/python3" in INSTALLER.read_text()
    assert "raw.githubusercontent.com/sanecmp/sanex/main" in INSTALLER.read_text()


@pytest.mark.parametrize(
    ("package_arguments", "expected_package"),
    [
        pytest.param([], "sanecmp-sanex", id="default-package"),
        pytest.param(["sanecmp-sanex==0.1.0"], "sanecmp-sanex==0.1.0", id="exact-version"),
    ],
)
def test_installer_stages_complete_tree_and_is_repeatable(
    tmp_path: Path, package_arguments: list[str], expected_package: str,
) -> None:
    fake_uv = tmp_path / "uv"
    uv_log = tmp_path / "uv.log"
    fake_uv.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$SANEX_FAKE_UV_LOG"
mkdir -p -- "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"
for entrypoint in sanex sanex-indicator sanex-window-agent; do
    printf '#!/bin/sh\\nexit 0\\n' > "$UV_TOOL_BIN_DIR/$entrypoint"
    chmod 0755 "$UV_TOOL_BIN_DIR/$entrypoint"
done
"""
    )
    fake_uv.chmod(0o755)
    destination = tmp_path / "root"
    environment = {"SANEX_FAKE_UV_LOG": f"{uv_log}"}
    command = [
        f"{INSTALLER}",
        "--destdir",
        f"{destination}",
        "--uv",
        f"{fake_uv}",
        "--python",
        f"{sys.executable}",
        *package_arguments,
    ]

    for attempt in range(2):
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert f"sanex staged successfully in {destination}." in result.stdout

    private_directories = (
        destination / "opt/sanex/config",
        destination / "opt/sanex/config/pki",
        destination / "opt/sanex/state",
        destination / "opt/sanex/state/accounts",
        destination / "var/log/sanex",
    )

    for directory in private_directories:
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    for directory in (
        destination / "opt/sanex/bundle",
        destination / "opt/sanex/bin",
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755

    for entrypoint in ("sanex", "sanex-indicator", "sanex-window-agent"):
        path = destination / "opt/sanex/bin" / entrypoint
        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR

    install_config = destination / "opt/sanex/config/install.json"
    assert stat.S_IMODE(install_config.stat().st_mode) == 0o600
    assert json.loads(install_config.read_text()) == {
        "uv": f"{fake_uv.resolve()}",
        "python": f"{Path(sys.executable).resolve()}",
    }
    installed_assets = {
        destination / "etc/systemd/system/sanex.service": UNIT,
        destination / "etc/systemd/system/sanex.path": PATH_UNIT,
        destination / "etc/logrotate.d/sanex": LOGROTATE,
        destination / "usr/share/icons/hicolor/scalable/status/sanex-symbolic.svg": ICON,
        destination / "usr/share/icons/hicolor/scalable/status/sanex-warning-symbolic.svg": WARNING_ICON,
        destination / "etc/xdg/autostart/sanex-indicator.desktop": AUTOSTART,
    }

    for installed, source in installed_assets.items():
        assert installed.read_bytes() == source.read_bytes()
        assert stat.S_IMODE(installed.stat().st_mode) == 0o644

    invocations = uv_log.read_text().splitlines()
    assert len(invocations) == 2
    assert all("tool install" in invocation for invocation in invocations)
    assert all("--force" in invocation for invocation in invocations)
    assert all(invocation.split()[-1] == expected_package for invocation in invocations)


def test_wheel_contains_runtime_only(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    output = tmp_path / "dist"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", f"{output}"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("sanecmp_sanex-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points = archive.read(
            next(name for name in names if name.endswith("entry_points.txt"))
        ).decode()

    assert not any(name.startswith("tests/") for name in names)
    assert not any("/__pycache__/" in name for name in names)
    assert "sanex = sanex.cli:main" in entry_points
    assert "sanex-indicator = sanex.indicator.app:main" in entry_points
    assert "sanex-window-agent = sanex.window_agent:main" in entry_points
    assert "sanex/locale/ru/LC_MESSAGES/sanex.mo" in names


def test_systemd_units_are_valid(tmp_path: Path) -> None:
    systemd_analyze = shutil.which("systemd-analyze")

    if systemd_analyze is None:
        pytest.skip("systemd-analyze is unavailable")

    unit_directory = tmp_path / "etc/systemd/system"
    bin_directory = tmp_path / "opt/sanex/bin"
    unit_directory.mkdir(parents=True)
    bin_directory.mkdir(parents=True)
    shutil.copy2(UNIT, unit_directory)
    shutil.copy2(PATH_UNIT, unit_directory)

    for name in (
        "sysinit.target",
        "basic.target",
        "multi-user.target",
        "network.target",
    ):
        (unit_directory / name).write_text("[Unit]\nDescription=Test stub\n")

    for name in ("dbus.service", "systemd-logind.service"):
        (unit_directory / name).write_text(
            "[Service]\nType=oneshot\nExecStart=/bin/true\n"
        )

    executable = bin_directory / "sanex"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    subprocess.run(
        [
            systemd_analyze,
            "verify",
            f"--root={tmp_path}",
            "sanex.service",
            "sanex.path",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_systemd_unit_preserves_update_and_accounting_requirements() -> None:
    content = UNIT.read_text()

    assert "ExecStart=/opt/sanex/bin/sanex service" in content
    assert "Restart=always" in content
    assert "ConditionPathExists=/opt/sanex/config/config.json" in content
    assert "ReadWritePaths=/opt/sanex /var/log/sanex" in content
    assert "StandardError=append:/var/log/sanex/sanex.log" in content
    assert "ProtectProc=" not in content
    assert "copytruncate" in LOGROTATE.read_text()


def test_path_unit_starts_service_after_registration() -> None:
    content = PATH_UNIT.read_text()

    assert "PathExists=/opt/sanex/config/config.json" in content
    assert "Unit=sanex.service" in content
    assert "WantedBy=multi-user.target" in content
    assert "systemctl enable --now sanex.path" in INSTALLER.read_text()


def test_indicator_starts_in_each_gnome_session() -> None:
    content = AUTOSTART.read_text()

    assert "Type=Application" in content
    assert "TryExec=/opt/sanex/bin/sanex-indicator" in content
    assert "Exec=/opt/sanex/bin/sanex-indicator" in content
    assert "OnlyShowIn=GNOME;" in content
    assert "X-GNOME-Autostart-enabled=true" in content
