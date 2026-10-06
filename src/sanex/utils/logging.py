"""Consistent private technical-log configuration."""

import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from logging.handlers import WatchedFileHandler
from pathlib import Path

from ..exceptions import StorageError
from .resources import close_resources


DEFAULT_LOG_PATH = Path("/var/log/sanex/sanex.log")
TECHNICAL_LOGGER_NAME = "sanex"
TECHNICAL_HANDLER_NAME = "sanex-technical-log"
TECHNICAL_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(path: Path = DEFAULT_LOG_PATH) -> WatchedFileHandler:
    """Send every sanex logger through one protected watched file."""
    app_logger = logging.getLogger(TECHNICAL_LOGGER_NAME)
    handler = None

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        handler = WatchedFileHandler(path, encoding="utf-8")
        os.chmod(path, 0o600)
        handler.set_name(TECHNICAL_HANDLER_NAME)
        formatter = logging.Formatter(
            TECHNICAL_LOG_FORMAT,
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        previous = tuple(
            configured
            for configured in app_logger.handlers
            if configured.get_name() == TECHNICAL_HANDLER_NAME
        )
        operations = []

        for configured in previous:
            app_logger.removeHandler(configured)
            operations.append(("close replaced technical log", configured.close))

        close_resources(*operations)
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False

    except BaseException as error:

        if handler is not None:
            app_logger.removeHandler(handler)
            close_resources(("close unconfigured technical log", handler.close))

        if isinstance(error, OSError):
            raise StorageError("configure technical log", path, error) from error

        raise

    return handler


@contextmanager
def logging_context(path: Path | None = DEFAULT_LOG_PATH) -> Iterator[None]:
    """Own CLI diagnostics and an optional technical log without changing root."""
    app_logger = logging.getLogger(TECHNICAL_LOGGER_NAME)
    original_handlers = tuple(app_logger.handlers)
    original_level = app_logger.level
    original_propagate = app_logger.propagate
    detached = tuple(
        handler for handler in original_handlers
        if handler.get_name() == TECHNICAL_HANDLER_NAME
    )
    diagnostic = logging.StreamHandler(sys.stderr)
    diagnostic.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
    owned: list[logging.Handler] = [diagnostic]

    def restore_logging() -> None:

        for position, handler in enumerate(original_handlers):

            if handler in detached and handler not in app_logger.handlers:
                app_logger.handlers.insert(position, handler)

        app_logger.setLevel(original_level)
        app_logger.propagate = original_propagate

    def close_handler(handler: logging.Handler) -> None:
        app_logger.removeHandler(handler)
        handler.close()

    try:

        for handler in detached:
            app_logger.removeHandler(handler)

        app_logger.addHandler(diagnostic)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False

        if path is not None:

            try:
                owned.append(configure_logging(path))

            except Exception:
                app_logger.exception("Unable to prepare technical log")
                raise

        yield

    finally:
        operations = []

        for handler in reversed(owned):
            operations.append(("close CLI log handler", partial(close_handler, handler)))

        close_resources(*operations, ("restore application logging", restore_logging))
