"""Tests for the sanex command-line interface."""

from importlib.metadata import version
from pathlib import Path

import pytest

from sanex import cli
from sanex.cli import main
from sanex.utils.logging import logging_context

import sys
from sanex.exceptions import ServiceError
from sanex.exceptions import StorageError
from sanex.utils import logging as log_module


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "logging_context", lambda path: logging_context(tmp_path / "sanex.log"))


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("usage: sanex ")
    assert captured.err == ""


def test_version_option_prints_distribution_version(
    capsys: pytest.CaptureFixture[str],
) -> None:

    with pytest.raises(SystemExit) as raised:
        main(["--version"])

    assert raised.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"sanex {version("sanex")}\n"
    assert captured.err == ""


def test_unknown_argument_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as raised:
        main(["--unknown"])

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments: --unknown" in captured.err


def test_service_command_runs_system_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "run_service", lambda: calls.append("service"))

    assert main(["service"]) == 0

    assert calls == ["service"]


def test_register_command_runs_explicit_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        cli,
        "run_registration",
        lambda code: calls.append(code),
    )

    assert main(["register", "ABCD-EFGH"]) == 0

    assert calls == ["ABCD-EFGH"]


def test_develop_commands_use_the_selected_isolated_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []
    monkeypatch.setattr(
        cli,
        "run_development_registration",
        lambda code, state_dir: calls.append(("register", code, state_dir)),
    )
    monkeypatch.setattr(
        cli,
        "run_development_service",
        lambda state_dir: calls.append(("service", state_dir)),
    )

    assert main(
        [
            "develop",
            "--state-dir",
            f"{tmp_path}",
            "register",
            "ABCD-EFGH",
        ]
    ) == 0
    assert main(
        ["develop", "--state-dir", f"{tmp_path}", "service"]
    ) == 0

    assert calls == [
        ("register", "ABCD-EFGH", tmp_path),
        ("service", tmp_path),
    ]


def test_develop_without_action_prints_its_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["develop"]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("usage: sanex develop ")
    assert captured.err == ""


def test_unexpected_failure_is_logged_and_propagated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "run_service", fail)

    with pytest.raises(RuntimeError, match="unexpected"):
        main(["service"])

    assert "Unexpected command failure" in capsys.readouterr().err


@pytest.mark.parametrize("terminal", [False, True], ids=["redirected", "terminal"])
def test_expected_failure_has_one_stderr_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], terminal: bool,
) -> None:

    def fail() -> None:
        raise ServiceError("service unavailable")

    monkeypatch.setattr(cli, "run_service", fail)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: terminal)

    assert main(["service"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("Command failed: service unavailable") == 1


def test_log_preparation_failure_prevents_command_execution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:

    calls = []

    def fail(path: Path) -> None:
        raise StorageError("configure technical log", path, PermissionError("permission denied"))

    monkeypatch.setattr(log_module, "configure_logging", fail)
    monkeypatch.setattr(cli, "run_service", lambda: calls.append("service"))

    assert main(["service"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Unable to prepare technical log" in captured.err
    assert "permission denied" in captured.err
    assert calls == []
    assert not (tmp_path / "sanex.log").exists()
