"""Tests for the internal window-agent command line."""

import pytest

from sanex.window_agent import create_parser

from unittest.mock import AsyncMock
from sanex import window_agent


def test_parser_accepts_inherited_channel_identity() -> None:
    arguments = create_parser().parse_args(
        ["--fd", "7", "--uid", "1001", "--session", "3"]
    )

    assert arguments.fd == 7
    assert arguments.uid == 1001
    assert arguments.session == "3"


def test_main_delegates_plain_values_to_service_runner(monkeypatch: pytest.MonkeyPatch) -> None:

    runner = AsyncMock()
    monkeypatch.setattr(window_agent, "run_agent", runner)

    assert window_agent.main(["--fd", "7", "--uid", "1001", "--session", "3"]) == 0

    runner.assert_awaited_once_with(7, 1001, "3")
