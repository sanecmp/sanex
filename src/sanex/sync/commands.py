"""Durable sanea command queue and terminal results."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sanelib.protocol import Command, CommandResult, CommandStatus

from ..exceptions import CommandStateError, ServiceError, StorageError
from ..model.validation import parse_json_model, require_unique
from ..storage.atomic import atomic_write_bytes, ensure_private_directory


DEFAULT_COMMAND_STATE_PATH = Path("/opt/sanex/state/commands.json")


class _CommandState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pending: tuple[Command, ...]
    results: tuple[CommandResult, ...]

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        require_unique(
            (command.ident for command in self.pending),
            "pending command ident",
        )
        require_unique(
            (result.ident for result in self.results),
            "command result ident",
        )
        overlap = {command.ident for command in self.pending} & {
            result.ident for result in self.results
        }

        if overlap:
            raise ValueError(
                f"command ident {min(overlap)} cannot be pending and completed"
            )

        return self


@dataclass(slots=True)
class CommandStore:
    """Persist pending commands and unacknowledged terminal results atomically."""

    path: Path = DEFAULT_COMMAND_STATE_PATH
    _state: _CommandState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._state = self._load()

    @property
    def pending(self) -> tuple[Command, ...]:
        return self._state.pending

    @property
    def results(self) -> tuple[CommandResult, ...]:
        return self._state.results

    def accept(
        self,
        sent_results: tuple[CommandResult, ...],
        received: tuple[Command, ...],
    ) -> None:
        """Acknowledge sent results and replace the pending server command set."""
        acknowledged = {result.ident for result in sent_results}
        results = tuple(
            result
            for result in self._state.results
            if result.ident not in acknowledged
        )
        result_idents = {result.ident for result in results}
        previous = {command.ident: command for command in self._state.pending}
        pending: list[Command] = []

        for command in received:

            if command.ident in result_idents:
                raise ServiceError(
                    f"sanea repeated completed command {command.ident} without acknowledgement"
                )

            known = previous.get(command.ident)

            if known is not None and known != command:
                raise ServiceError(
                    f"command ident {command.ident} was reused for different content"
                )

            pending.append(command)

        self._save(_CommandState(pending=tuple(pending), results=results))

    def record(self, result: CommandResult) -> None:
        """Replace one pending command with its terminal result."""
        pending_by_ident = {command.ident: command for command in self._state.pending}

        if result.ident not in pending_by_ident:
            raise ServiceError(f"command {result.ident} is not pending")

        results_by_ident = {item.ident: item for item in self._state.results}
        known = results_by_ident.get(result.ident)

        if known is not None and known != result:
            raise ServiceError(
                f"command {result.ident} already has a different terminal result"
            )

        results_by_ident[result.ident] = result
        pending = tuple(
            command
            for command in self._state.pending
            if command.ident != result.ident
        )
        results = tuple(sorted(results_by_ident.values(), key=lambda item: item.ident))
        self._save(_CommandState(pending=pending, results=results))

    def _load(self) -> _CommandState:
        try:
            data = self.path.read_bytes()

        except FileNotFoundError:
            return _CommandState(pending=(), results=())

        except OSError as error:
            raise StorageError("read", self.path, error) from error

        return parse_json_model(_CommandState, data, CommandStateError)

    def _save(self, state: _CommandState) -> None:
        encoded = state.model_dump_json().encode()
        ensure_private_directory(self.path.parent)
        atomic_write_bytes(self.path, encoded)
        self._state = state


class CommandHandler(Protocol):
    """Execute one validated command and return its terminal result."""

    async def run(self, command: Command) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class CommandExecutor:
    """Execute durable commands sequentially, leaving update commands last."""

    store: CommandStore
    handlers: Mapping[str, CommandHandler]

    async def run(self) -> None:
        """Execute the current pending snapshot and persist every result."""
        commands = tuple(
            sorted(
                enumerate(self.store.pending),
                key=lambda item: (item[1].type == "update", item[0]),
            )
        )

        for position, command in commands:
            handler = self.handlers.get(command.type)

            if handler is None:
                message = f"unsupported command type: {command.type}"[:1_024]
                result = CommandResult(
                    ident=command.ident,
                    status=CommandStatus.FAILED,
                    error=message,
                )

            else:
                result = await handler.run(command)

                if result.ident != command.ident:
                    raise ServiceError(
                        f"handler returned result {result.ident} for command {command.ident}"
                    )

            self.store.record(result)
