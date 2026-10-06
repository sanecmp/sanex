"""Tests for the on-demand About helper lifecycle."""

import asyncio

import pytest

from sanex.indicator.about import AboutDialog
from sanex.indicator.presentation import IndicatorPresentationBuilder

from unittest.mock import AsyncMock
from sanex.indicator import about
from tests.sanex.resource_fakes import FakeProcess


@pytest.mark.asyncio
async def test_opens_only_one_dialog_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    finished = asyncio.Event()

    class FakeProcess:
        returncode = None

        async def wait(self) -> int:
            await finished.wait()
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            finished.set()

    async def create_process(*arguments: str) -> FakeProcess:
        calls.append(arguments)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    presentation = IndicatorPresentationBuilder("1.2.3").build(None)
    dialog = AboutDialog(presentation.about)

    dialog.open()
    dialog.open()
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert "--info" in calls[0]
    assert any("Version 1.2.3" in argument for argument in calls[0])
    assert any("https://github.com/sanecmp" in argument for argument in calls[0])

    await dialog.close()
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cancellation_terminates_and_waits_for_helper(monkeypatch: pytest.MonkeyPatch) -> None:

    process = FakeProcess(blocked=True)
    monkeypatch.setattr(about.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    dialog = AboutDialog(IndicatorPresentationBuilder("1.2.3").build(None).about)
    assert process.wait_count == 0

    dialog.open()
    await asyncio.sleep(0)
    await dialog.close()

    assert process.terminated
    assert process.returncode == 0
    assert process.wait_count == 2
    await dialog.close()
    assert process.wait_count == 2
