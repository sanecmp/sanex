"""Persistent storage for the current sanex configuration snapshot."""

from dataclasses import dataclass
from pathlib import Path

from sanelib.protocol import Config

from ..exceptions import StorageError
from ..model.config import parse_config
from .atomic import atomic_write_bytes


DEFAULT_CONFIG_PATH = Path("/opt/sanex/config/config.json")


@dataclass(frozen=True, slots=True)
class ConfigStore:
    """Validate and atomically store one current configuration snapshot."""

    path: Path

    def load(self) -> Config | None:
        """Load the current snapshot, or return ``None`` when it does not exist."""
        try:
            data = self.path.read_bytes()

        except FileNotFoundError:
            return None

        except OSError as error:
            raise StorageError("read", self.path, error) from error

        return parse_config(data)

    def save(self, data: str | bytes | bytearray) -> Config:
        """Validate *data*, durably replace the snapshot and return its model."""
        config = parse_config(data)
        encoded = data.encode() if isinstance(data, str) else bytes(data)
        atomic_write_bytes(self.path, encoded)
        return config
