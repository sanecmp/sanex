"""Tests for validated uv process replacement updates."""

import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from sanelib.protocol import Command, CommandStatus

from sanex.exceptions import UpdateError
from sanex.sync.commands import CommandStore
from sanex.sync.update import (
    InstallConfigStore,
    RootExecutableValidator,
    UpdateHandler,
    UpdateRecovery,
    UpdateState,
    UpdateStateStore,
)


class FakeExecutableValidator:
    def validate(self, path: str) -> str:
        return path


class RecordingProcessReplacer:
    def __init__(self) -> None:
        self.call: tuple[str, tuple[str, ...], dict[str, str]] | None = None

    def replace(
        self,
        executable: str,
        arguments: Sequence[str],
        environment: Mapping[str, str],
    ) -> None:
        self.call = (executable, tuple(arguments), dict(environment))


def write_install(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload))


@pytest.mark.parametrize(
    ("file_uid", "file_mode", "directory_mode", "message"),
    [
        pytest.param(0, stat.S_IFREG | 0o755, stat.S_IFDIR | 0o755, None, id="protected"),
        pytest.param(1001, stat.S_IFREG | 0o755, stat.S_IFDIR | 0o755, "executable is not protected", id="wrong-owner"),
        pytest.param(0, stat.S_IFREG | 0o775, stat.S_IFDIR | 0o755, "executable is not protected", id="writable-file"),
        pytest.param(0, stat.S_IFREG | 0o644, stat.S_IFDIR | 0o755, "executable is not protected", id="non-executable"),
        pytest.param(0, stat.S_IFREG | 0o755, stat.S_IFDIR | 0o775, "directory is not protected", id="writable-directory"),
    ],
)
def test_requires_root_protected_executable_path(
    monkeypatch: pytest.MonkeyPatch, file_uid: int, file_mode: int, directory_mode: int, message: str | None,
) -> None:
    candidate = Path("/synthetic/bin/uv")

    def metadata(path: Path) -> os.stat_result:
        mode = file_mode if path == candidate else directory_mode
        uid = file_uid if path == candidate else 0
        return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: path)
    monkeypatch.setattr(Path, "stat", metadata)
    validator = RootExecutableValidator()

    if message is None:
        assert validator.validate(f"{candidate}") == f"{candidate}"

    else:

        with pytest.raises(UpdateError, match=message):
            validator.validate(f"{candidate}")


def test_recovers_successful_update_from_installed_version(tmp_path: Path) -> None:
    command = Command(
        ident=92,
        type="update",
        payload={
            "version": "2.0",
            "index_url": "https://packages.example.test/simple",
        },
    )
    commands = CommandStore(tmp_path / "commands.json")
    commands.accept((), (command,))
    states = UpdateStateStore(tmp_path / "update.json")
    states.save(
        UpdateState(
            command_ident=92,
            version="2.0",
            index_url="https://packages.example.test/simple",
            attempts=1,
            phase="installing",
        )
    )

    UpdateRecovery(commands, states, lambda: "2.0").reconcile()

    assert not commands.pending
    assert len(commands.results) == 1
    assert commands.results[0].status is CommandStatus.DONE
    assert states.load() is None


def test_removes_update_state_after_command_cancellation(tmp_path: Path) -> None:
    commands = CommandStore(tmp_path / "commands.json")
    states = UpdateStateStore(tmp_path / "update.json")
    states.save(
        UpdateState(
            command_ident=92,
            version="2.0",
            index_url="https://packages.example.test/simple",
            attempts=1,
            phase="installing",
        )
    )

    UpdateRecovery(commands, states, lambda: "1.0").reconcile()

    assert states.load() is None


@pytest.mark.asyncio
async def test_prepares_exact_uv_replacement_and_durable_attempt(
    tmp_path: Path,
    install_payload: dict[str, Any],
) -> None:
    install_path = tmp_path / "install.json"
    state_path = tmp_path / "update.json"
    write_install(install_path, install_payload)
    prepared = False

    async def before_exec() -> None:
        nonlocal prepared
        prepared = True

    replacer = RecordingProcessReplacer()
    handler = UpdateHandler(
        before_exec=before_exec,
        installed_version=lambda: "0.1.0",
        install_store=InstallConfigStore(install_path),
        state_store=UpdateStateStore(state_path),
        executable_validator=FakeExecutableValidator(),
        process_replacer=replacer,
        environment=lambda: {
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://proxy.test",
            "UV_INDEX_URL": "https://untrusted.test/simple",
        },
    )
    command = Command(
        ident=92,
        type="update",
        payload={
            "version": "0.2.0.dev3+g4f81a2c",
            "index_url": "https://packages.example.test/simple",
        },
    )

    result = await handler.run(command)

    assert prepared
    assert result.status is CommandStatus.FAILED
    assert result.error == "uv process replacement returned"
    assert replacer.call is not None
    executable, arguments, environment = replacer.call
    assert executable == install_payload["uv"]
    assert arguments == (
        install_payload["uv"],
        "tool",
        "install",
        "--force",
        "--no-cache",
        "--no-config",
        "--no-sources",
        "--no-managed-python",
        "--no-python-downloads",
        "--no-progress",
        "--color",
        "never",
        "--index-strategy",
        "first-index",
        "--default-index",
        "https://packages.example.test/simple",
        "--python",
        install_payload["python"],
        "sanex==0.2.0.dev3+g4f81a2c",
    )
    assert environment["UV_TOOL_DIR"] == "/opt/sanex/bundle"
    assert environment["UV_TOOL_BIN_DIR"] == "/opt/sanex/bin"
    assert "UV_INDEX_URL" not in environment
    assert environment["HTTPS_PROXY"] == "http://proxy.test"
    state = UpdateStateStore(state_path).load()
    assert state is not None
    assert state.command_ident == 92
    assert state.attempts == 1


@pytest.mark.asyncio
async def test_matching_installed_version_completes_without_exec(
    tmp_path: Path,
) -> None:
    replacer = RecordingProcessReplacer()
    handler = UpdateHandler(
        before_exec=lambda: _noop(),
        installed_version=lambda: "1.2.3",
        install_store=InstallConfigStore(tmp_path / "missing.json"),
        state_store=UpdateStateStore(tmp_path / "update.json"),
        executable_validator=FakeExecutableValidator(),
        process_replacer=replacer,
    )
    command = Command(
        ident=5,
        type="update",
        payload={"version": "1.2.3", "index_url": "https://pypi.org/simple"},
    )

    result = await handler.run(command)

    assert result.status is CommandStatus.DONE
    assert result.error is None
    assert replacer.call is None


@pytest.mark.asyncio
async def test_rejects_untrusted_index_without_exposing_it(tmp_path: Path) -> None:
    handler = UpdateHandler(
        before_exec=lambda: _noop(),
        installed_version=lambda: "1.0",
        install_store=InstallConfigStore(tmp_path / "missing.json"),
        state_store=UpdateStateStore(tmp_path / "update.json"),
    )
    command = Command(
        ident=6,
        type="update",
        payload={
            "version": "2.0",
            "index_url": "https://user:secret@example.test/simple",
        },
    )

    result = await handler.run(command)

    assert result.status is CommandStatus.FAILED
    assert result.error == "invalid update command"
    assert "secret" not in result.error


@pytest.mark.asyncio
async def test_rejects_corrupt_update_state(tmp_path: Path) -> None:
    install_path = tmp_path / "install.json"
    state_path = tmp_path / "update.json"
    write_install(
        install_path,
        {"uv": "/usr/bin/uv", "python": "/usr/bin/python3.12"},
    )
    state_path.write_text("{}")
    handler = UpdateHandler(
        before_exec=lambda: _noop(),
        installed_version=lambda: "1.0",
        install_store=InstallConfigStore(install_path),
        state_store=UpdateStateStore(state_path),
        executable_validator=FakeExecutableValidator(),
    )
    command = Command(
        ident=7,
        type="update",
        payload={"version": "2.0", "index_url": "https://pypi.org/simple"},
    )

    result = await handler.run(command)

    assert result.status is CommandStatus.FAILED
    assert result.error == "invalid update state"


async def _noop() -> None:
    return None
