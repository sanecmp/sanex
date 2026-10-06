"""Bounded technical-log snapshots for sanea diagnostics."""

from dataclasses import dataclass
from pathlib import Path

from sanelib.protocol import MAX_LOG_TAIL_SIZE

from ..exceptions import StorageError
from ..utils.logging import DEFAULT_LOG_PATH


@dataclass(frozen=True, slots=True)
class TechnicalLog:
    """Read an exact bounded tail without returning a partial first line."""

    path: Path = DEFAULT_LOG_PATH
    max_size: int = MAX_LOG_TAIL_SIZE

    def __post_init__(self) -> None:

        if self.max_size <= 0:
            raise ValueError("max_size must be positive")

    def tail(self) -> bytes:
        """Return at most ``max_size`` bytes, aligned to a complete line."""
        try:
            size = self.path.stat().st_size

            with self.path.open("rb") as stream:
                truncated = size > self.max_size

                if truncated:
                    stream.seek(size - self.max_size - 1)
                    data = stream.read(self.max_size + 1)

                else:
                    data = stream.read(self.max_size)

        except FileNotFoundError:
            return b""

        except OSError as error:
            raise StorageError("read", self.path, error) from error

        if not truncated:
            return data

        if data.startswith(b"\n"):
            return data[1:]

        line_end = data.find(b"\n")
        return data[line_end + 1 :] if line_end >= 0 else b""
