"""Tests for the composed desktop indicator application."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from sanex.indicator import app
from sanex.indicator.app import IndicatorApplication
from sanex.indicator.presentation import IndicatorPresentation, IndicatorPresentationBuilder
from sanex.model.indicator import IndicatorStatus

from sanex.exceptions import IndicatorError
from sanex.utils import logging as log_module
from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import Mock


class FakeClient:
    def __init__(self, status: IndicatorStatus) -> None:
        self.status = status
        self.ran = False

    async def run(
        self, stop_requested: asyncio.Event, receive: Callable[[IndicatorStatus | None], None],
    ) -> None:
        self.ran = True
        receive(self.status)


class SequenceClient:
    def __init__(self, statuses: tuple[IndicatorStatus | None, ...]) -> None:
        self.statuses = statuses

    async def run(
        self, stop_requested: asyncio.Event, receive: Callable[[IndicatorStatus | None], None],
    ) -> None:

        for status in self.statuses:
            receive(status)

            if stop_requested.is_set():
                return


class FakeDesktop:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.started = False
        self.closed = False
        self.presentations = []

    async def start(self) -> bool:
        self.started = True
        return self.acquired

    def update(self, presentation: IndicatorPresentation) -> None:
        self.presentations.append(presentation)

    def close(self) -> None:
        self.closed = True


class FakeAbout:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_runs_client_and_updates_desktop_presentation() -> None:
    status = IndicatorStatus(remaining=3600, break_duration=7200)
    client = FakeClient(status)
    desktop = FakeDesktop()
    about = FakeAbout()
    application = IndicatorApplication(
        client=client,
        presentation_builder=IndicatorPresentationBuilder("1.2.3"),
        desktop=desktop,
        about=about,
        install_signal_handlers=False,
    )

    await application.run()

    assert client.ran
    assert desktop.started
    assert desktop.presentations[0].status_label == "Time remaining: 1 h"
    assert desktop.closed
    assert about.closed


@pytest.mark.asyncio
async def test_skips_client_when_another_indicator_is_running() -> None:
    client = FakeClient(IndicatorStatus(remaining=None, break_duration=0))
    desktop = FakeDesktop(acquired=False)
    about = FakeAbout()
    application = IndicatorApplication(
        client=client,
        presentation_builder=IndicatorPresentationBuilder("1.2.3"),
        desktop=desktop,
        about=about,
        install_signal_handlers=False,
    )

    await application.run()

    assert not client.ran
    assert desktop.closed
    assert about.closed


@pytest.mark.asyncio
async def test_restarts_updated_indicator_after_service_disconnect() -> None:
    status = IndicatorStatus(remaining=3600, break_duration=7200)
    desktop = FakeDesktop()
    about = FakeAbout()
    versions = iter(("1.2.3", "1.2.4"))
    replacements = []

    def replace_process() -> None:
        assert desktop.closed
        assert about.closed
        replacements.append(True)

    application = IndicatorApplication(
        client=SequenceClient((status, None)),
        presentation_builder=IndicatorPresentationBuilder("1.2.3"),
        desktop=desktop,
        about=about,
        running_version="1.2.3",
        read_installed_version=lambda: next(versions),
        replace_process=replace_process,
        install_signal_handlers=False,
    )

    await application.run()

    assert replacements == [True]
    assert len(desktop.presentations) == 1


def test_parser_accepts_development_socket() -> None:
    path = Path("/tmp/sanex-development.sock")

    arguments = app.create_parser().parse_args(
        ["--socket", f"{path}", "--session", "3"]
    )

    assert arguments.socket == path
    assert arguments.session == "3"


def test_replaces_process_through_original_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        app.sys,
        "argv",
        ["bin/sanex-indicator", "--session", "3"],
    )
    monkeypatch.setattr(
        app.os,
        "execv",
        lambda executable, arguments: calls.append((executable, arguments)),
    )

    app.replace_indicator_process()

    executable = f"{tmp_path}/bin/sanex-indicator"
    assert calls == [
        (executable, [executable, "--session", "3"]),
    ]


def test_main_runs_indicator_with_selected_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def run_indicator(socket_path: Path, sess_ident: str | None) -> None:
        calls.append((socket_path, sess_ident))

    monkeypatch.setattr(app, "run_indicator", run_indicator)

    assert app.main(["--socket", "/tmp/status.sock", "--session", "3"]) == 0
    assert calls == [(Path("/tmp/status.sock"), "3")]


def test_indicator_failure_is_logged_to_stderr_without_technical_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:

    async def fail(socket_path: Path, sess_ident: str | None) -> None:
        raise IndicatorError("connect", "desktop unavailable")

    def forbid_technical_log(path: Path) -> None:
        pytest.fail("The user indicator must not open the technical log")

    monkeypatch.setattr(app, "run_indicator", fail)
    monkeypatch.setattr(log_module, "configure_logging", forbid_technical_log)

    assert app.main(["--session", "3"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("Indicator failed: indicator connect failed: desktop unavailable") == 1


@pytest.mark.asyncio
async def test_about_failure_does_not_skip_desktop_cleanup() -> None:

    desktop = FakeDesktop()
    about = FakeAbout()
    about.close = AsyncMock(side_effect=RuntimeError("about cleanup failed"))
    application = IndicatorApplication(
        client=FakeClient(IndicatorStatus(remaining=None, break_duration=0)),
        presentation_builder=IndicatorPresentationBuilder("1.2.3"),
        desktop=desktop,
        about=about,
        install_signal_handlers=False,
    )

    with pytest.raises(RuntimeError, match="about cleanup failed"):
        await application.run()

    assert desktop.closed


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_application_factory_closes_logind_after_connect_failure(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:

    failure = asyncio.CancelledError("connect failed") if cancel else OSError("connect failed")
    logind = SimpleNamespace(connect=AsyncMock(side_effect=failure), close=Mock())
    monkeypatch.setattr(app, "LogindClient", lambda: logind)

    with pytest.raises(type(failure), match="connect failed"):
        await app.create_application(Path("/test/indicator.sock"))

    logind.close.assert_called_once_with()
