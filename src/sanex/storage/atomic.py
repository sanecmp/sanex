"""Durable atomic replacement of individual files."""

import os
import tempfile
from pathlib import Path

from ..exceptions import StorageError


DEFAULT_FILE_MODE = 0o600
DEFAULT_DIRECTORY_MODE = 0o700


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = DEFAULT_FILE_MODE) -> None:
    """Durably replace *path* with *data* using a temporary sibling file."""
    path = Path(path)
    temporary_path: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, mode)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1

        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        fsync_directory(path.parent)

    except OSError as error:
        raise StorageError("atomically write", path, error) from error

    finally:

        if descriptor >= 0:
            os.close(descriptor)

        if temporary_path is not None:
            try:
                temporary_path.unlink()

            except FileNotFoundError:
                pass


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_private_directory(path: Path, *, mode: int = DEFAULT_DIRECTORY_MODE) -> None:
    """Create a directory tree and enforce the requested mode on its leaf."""
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        path.chmod(mode)

    except OSError as error:
        raise StorageError("prepare directory", path, error) from error


def atomic_move(source: Path, target: Path) -> None:
    """Atomically move a file and durably record both directory changes."""
    try:
        os.replace(source, target)
        fsync_directory(target.parent)

        if source.parent != target.parent:
            fsync_directory(source.parent)

    except OSError as error:
        raise StorageError(f"move to {target}", source, error) from error
