"""Tests for durable sanea command state."""

import json
from pathlib import Path
from typing import Any

import pytest
from sanelib.protocol import Command, CommandResult, CommandStatus

from sanex.exceptions import ServiceError
from sanex.sync.commands import CommandExecutor, CommandStore
from sanex.sync.protocol import parse_sync_response


def test_persists_commands_results_acknowledgements_and_cancellation(
    tmp_path: Path,
    sync_payload: dict[str, Any],
) -> None:
    path = tmp_path / "commands.json"
    command = parse_sync_response(json.dumps(sync_payload["response"])).commands[0]
    store = CommandStore(path)

    store.accept((), (command,))
    assert CommandStore(path).pending == (command,)

    result = CommandResult(ident=command.ident, status=CommandStatus.DONE, error=None)
    store.record(result)
    assert CommandStore(path).results == (result,)

    store.accept((result,), ())
    assert not CommandStore(path).pending
    assert not CommandStore(path).results


def test_rejects_reused_pending_command_ident(
    tmp_path: Path,
    sync_payload: dict[str, Any],
) -> None:
    command = parse_sync_response(json.dumps(sync_payload["response"])).commands[0]
    store = CommandStore(tmp_path / "commands.json")
    store.accept((), (command,))
    changed = command.model_copy(update={"type": "different"})

    with pytest.raises(ServiceError, match="reused for different content"):
        store.accept((), (changed,))


class RecordingHandler:
    def __init__(self, calls: list[int]) -> None:
        self.calls = calls

    async def run(self, command: Command) -> CommandResult:
        self.calls.append(command.ident)
        return CommandResult(
            ident=command.ident,
            status=CommandStatus.DONE,
            error=None,
        )


@pytest.mark.asyncio
async def test_executor_runs_commands_sequentially_with_update_last(
    tmp_path: Path,
) -> None:
    regular = Command(ident=2, type="regular", payload={})
    update = Command(ident=1, type="update", payload={})
    store = CommandStore(tmp_path / "commands.json")
    store.accept((), (update, regular))
    calls: list[int] = []
    handler = RecordingHandler(calls)

    await CommandExecutor(
        store,
        {"regular": handler, "update": handler},
    ).run()

    assert calls == [2, 1]
    assert [result.ident for result in store.results] == [1, 2]
    assert not store.pending


@pytest.mark.asyncio
async def test_executor_records_unknown_command_as_failed(tmp_path: Path) -> None:
    command = Command(ident=7, type="future-command", payload={})
    store = CommandStore(tmp_path / "commands.json")
    store.accept((), (command,))

    await CommandExecutor(store, {}).run()

    assert store.results == (
        CommandResult(
            ident=7,
            status=CommandStatus.FAILED,
            error="unsupported command type: future-command",
        ),
    )
