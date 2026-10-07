"""Validation and process replacement for sanex update commands."""

import os
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator
from sanelib.protocol import Command, CommandResult, CommandStatus, UpdateCommandPayload

from ..exceptions import (
    SaneaException,
    StorageError,
    UpdateConfigError,
    UpdateError,
    UpdateStateError,
)
from ..model.validation import NonEmptyString, NonNegativeInt, parse_json_model
from ..storage.atomic import (
    atomic_write_bytes,
    ensure_private_directory,
    fsync_directory,
)

DEFAULT_INSTALL_CONFIG_PATH = Path("/opt/sanex/config/install.json")
DEFAULT_UPDATE_STATE_PATH = Path("/opt/sanex/state/update.json")
DEFAULT_UPDATE_LOG_PATH = Path("/var/log/sanex/sanex.log")
DEFAULT_TOOL_DIR = "/opt/sanex/bundle"
DEFAULT_TOOL_BIN_DIR = "/opt/sanex/bin"


class _UpdateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InstallConfig(_UpdateModel):
    """Absolute system tool paths recorded during installation."""

    uv: NonEmptyString
    python: NonEmptyString

    @field_validator("uv", "python")
    @classmethod
    def validate_absolute_path(cls, value: str) -> str:

        if not Path(value).is_absolute():
            raise ValueError("tool path must be absolute")

        return value


class UpdateState(UpdateCommandPayload):
    """Durable marker interpreted after systemd restarts sanex."""

    command_ident: NonNegativeInt
    version: NonEmptyString
    index_url: NonEmptyString
    attempts: int
    phase: Literal["installing"]

    @field_validator("attempts")
    @classmethod
    def validate_attempts(cls, value: int) -> int:

        if isinstance(value, bool) or value < 1:
            raise ValueError("attempts must be a positive integer")

        return value


@dataclass(frozen=True, slots=True)
class InstallConfigStore:
    path: Path = DEFAULT_INSTALL_CONFIG_PATH

    def load(self) -> InstallConfig:
        try:
            data = self.path.read_bytes()

        except OSError as error:
            raise StorageError("read", self.path, error) from error

        return parse_json_model(InstallConfig, data, UpdateConfigError)


@dataclass(frozen=True, slots=True)
class UpdateStateStore:
    path: Path = DEFAULT_UPDATE_STATE_PATH

    def load(self) -> UpdateState | None:
        try:
            data = self.path.read_bytes()

        except FileNotFoundError:
            return None

        except OSError as error:
            raise StorageError("read", self.path, error) from error

        return parse_json_model(UpdateState, data, UpdateStateError)

    def save(self, state: UpdateState) -> None:
        ensure_private_directory(self.path.parent)
        atomic_write_bytes(self.path, state.model_dump_json().encode())

    def delete(self) -> None:
        try:
            self.path.unlink()

        except FileNotFoundError:
            return

        except OSError as error:
            raise StorageError("remove", self.path, error) from error

        try:
            fsync_directory(self.path.parent)

        except OSError as error:
            raise StorageError("synchronize directory", self.path.parent, error) from error


class UpdateCommandStore(Protocol):
    @property
    def pending(self) -> tuple[Command, ...]: ...

    def record(self, result: CommandResult) -> None: ...


@dataclass(frozen=True, slots=True)
class UpdateRecovery:
    """Reconcile a persisted uv attempt with commands and installed version."""

    command_store: UpdateCommandStore
    state_store: UpdateStateStore
    installed_version: Callable[[], str]

    def reconcile(self) -> None:
        state = self.state_store.load()

        if state is None:
            return

        command = next(
            (
                item
                for item in self.command_store.pending
                if item.ident == state.command_ident
            ),
            None,
        )

        if command is None:
            self.state_store.delete()
            return

        try:
            payload = UpdateCommandPayload.model_validate(command.payload)

        except ValueError as error:
            raise UpdateStateError(
                f"{self.state_store.path}",
                "persisted update refers to an invalid command",
            ) from error

        if (
            command.type != "update"
            or payload.version != state.version
            or payload.index_url != state.index_url
        ):
            raise UpdateStateError(
                f"{self.state_store.path}",
                "persisted update does not match its pending command",
            )

        if self.installed_version() != state.version:
            return

        self.command_store.record(
            CommandResult(
                ident=command.ident,
                status=CommandStatus.DONE,
                error=None,
            )
        )
        self.state_store.delete()


class ExecutableValidator(Protocol):
    def validate(self, path: str) -> str: ...


@dataclass(frozen=True, slots=True)
class RootExecutableValidator:
    """Reject executables replaceable by non-root users."""

    required_uid: int = 0

    def validate(self, path: str) -> str:
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()

        except OSError as error:
            raise UpdateError("configured update executable is unavailable") from error

        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.required_uid
            or metadata.st_mode & 0o022
            or not metadata.st_mode & 0o111
        ):
            raise UpdateError("configured update executable is not protected")

        self._validate_directories(candidate.parent)
        self._validate_directories(resolved.parent)
        return f"{resolved}"

    def _validate_directories(self, directory: Path) -> None:
        current = directory

        while True:
            try:
                metadata = current.stat()

            except OSError as error:
                raise UpdateError("update executable directory is unavailable") from error

            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self.required_uid
                or metadata.st_mode & 0o022
            ):
                raise UpdateError("update executable directory is not protected")

            if current.parent == current:
                return

            current = current.parent


class ProcessReplacer(Protocol):
    def replace(
        self,
        executable: str,
        arguments: Sequence[str],
        environment: Mapping[str, str],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecProcessReplacer:
    """Redirect diagnostics and replace sanex with uv."""

    log_path: Path = DEFAULT_UPDATE_LOG_PATH

    def replace(
        self,
        executable: str,
        arguments: Sequence[str],
        environment: Mapping[str, str],
    ) -> None:
        ensure_private_directory(self.log_path.parent)
        descriptor = -1
        try:
            descriptor = os.open(
                self.log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            os.dup2(descriptor, 1)
            os.dup2(descriptor, 2)
            os.close(descriptor)
            descriptor = -1
            os.execve(executable, list(arguments), dict(environment))

        except OSError as error:
            raise UpdateError("unable to replace sanex with uv") from error

        finally:

            if descriptor >= 0:
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class UpdateHandler:
    """Validate an update and replace the current service process with uv."""

    before_exec: Callable[[], Awaitable[None]]
    installed_version: Callable[[], str]
    install_store: InstallConfigStore = field(default_factory=InstallConfigStore)
    state_store: UpdateStateStore = field(default_factory=UpdateStateStore)
    executable_validator: ExecutableValidator = field(
        default_factory=RootExecutableValidator
    )
    process_replacer: ProcessReplacer = field(default_factory=ExecProcessReplacer)
    environment: Callable[[], Mapping[str, str]] = lambda: os.environ
    tool_dir: str = DEFAULT_TOOL_DIR
    tool_bin_dir: str = DEFAULT_TOOL_BIN_DIR

    async def run(self, command: Command) -> CommandResult:

        if command.type != "update":
            return self._failed(command.ident, "invalid update command")

        try:
            payload = UpdateCommandPayload.model_validate(command.payload)

        except ValueError:
            return self._failed(command.ident, "invalid update command")

        if self.installed_version() == payload.version:
            self.state_store.delete()
            return CommandResult(
                ident=command.ident,
                status=CommandStatus.DONE,
                error=None,
            )

        try:
            install = self.install_store.load()
            uv = self.executable_validator.validate(install.uv)
            python = self.executable_validator.validate(install.python)

        except (StorageError, UpdateConfigError, UpdateError):
            return self._failed(command.ident, "invalid install configuration")

        try:
            previous = self.state_store.load()

        except (StorageError, UpdateStateError):
            return self._failed(command.ident, "invalid update state")

        attempts = 1

        if (
            previous is not None
            and previous.command_ident == command.ident
            and previous.version == payload.version
            and previous.index_url == payload.index_url
        ):
            attempts = previous.attempts + 1

        try:
            await self.before_exec()
            self.state_store.save(
                UpdateState(
                    command_ident=command.ident,
                    version=payload.version,
                    index_url=payload.index_url,
                    attempts=attempts,
                    phase="installing",
                )
            )
            self.process_replacer.replace(
                uv,
                self._arguments(uv, python, payload),
                self._environment(),
            )

        except (OSError, SaneaException):
            try:
                self.state_store.delete()

            except StorageError:
                pass

            return self._failed(command.ident, "unable to start uv update")

        return self._failed(command.ident, "uv process replacement returned")

    @staticmethod
    def _arguments(
        uv: str,
        python: str,
        payload: UpdateCommandPayload,
    ) -> tuple[str, ...]:
        return (
            uv,
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
            payload.index_url,
            "--python",
            python,
            f"sanecmp-sanex=={payload.version}",
        )

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in self.environment().items()
            if not key.startswith("UV_")
        }
        environment["UV_TOOL_DIR"] = self.tool_dir
        environment["UV_TOOL_BIN_DIR"] = self.tool_bin_dir
        return environment

    @staticmethod
    def _failed(ident: int, message: str) -> CommandResult:
        return CommandResult(
            ident=ident,
            status=CommandStatus.FAILED,
            error=message,
        )
