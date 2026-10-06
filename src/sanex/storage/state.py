"""Persistent per-account runtime state."""

from dataclasses import dataclass
from pathlib import Path

from ..exceptions import RuntimeStateError, StorageError
from ..model.state import RuntimeState, parse_runtime_state
from .atomic import atomic_write_bytes, ensure_private_directory
from .paths import DEFAULT_ACCOUNTS_STATE_ROOT


@dataclass(frozen=True, slots=True)
class RuntimeStateStore:
    """Atomically store mutable runtime state in separate UID directories."""

    accounts_root: Path = DEFAULT_ACCOUNTS_STATE_ROOT

    def path_for(self, uid: int) -> Path:
        """Return the runtime-state path for one local UID."""
        return self.accounts_root / f"{uid}" / "runtime.json"

    def load(self, uid: int) -> RuntimeState | None:
        """Load one account state, or return ``None`` when it does not exist."""
        path = self.path_for(uid)
        try:
            data = path.read_bytes()

        except FileNotFoundError:
            return None

        except OSError as error:
            raise StorageError("read", path, error) from error

        return parse_runtime_state(data)

    def save(self, uid: int, state: RuntimeState) -> RuntimeState:
        """Revalidate and atomically save one account's mutable state."""
        try:
            encoded = state.model_dump_json().encode()

        except (TypeError, ValueError) as error:
            raise RuntimeStateError("$", f"state is not JSON-compatible: {error}") from error

        validated = parse_runtime_state(encoded)
        path = self.path_for(uid)
        ensure_private_directory(path.parent)
        atomic_write_bytes(path, encoded)
        return validated
