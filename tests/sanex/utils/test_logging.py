"""Technical-log ownership, permissions and CLI diagnostic lifetimes."""

import logging
import stat
from collections.abc import Iterator
from logging.handlers import WatchedFileHandler
from pathlib import Path

import pytest

from sanex.exceptions import StorageError
from sanex.utils import logging as log_module
from sanex.utils.logging import (
    TECHNICAL_HANDLER_NAME,
    TECHNICAL_LOGGER_NAME,
    configure_logging,
    logging_context,
)


@pytest.fixture
def app_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger(TECHNICAL_LOGGER_NAME)
    original_handlers = tuple(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate

    try:
        yield logger

    finally:

        for handler in tuple(logger.handlers):

            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()

        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_configures_one_private_watched_log_handler(tmp_path: Path, app_logger: logging.Logger) -> None:
    path = tmp_path / "log" / "sanex.log"
    configure_logging(path)
    configure_logging(path)
    logging.getLogger("sanex.test").info("Configuration applied")
    handlers = tuple(
        handler for handler in app_logger.handlers
        if handler.get_name() == TECHNICAL_HANDLER_NAME
    )

    assert len(handlers) == 1
    assert isinstance(handlers[0], WatchedFileHandler)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().count(" INFO sanex.test: Configuration applied\n") == 1


def test_closes_file_handler_when_permissions_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_logger: logging.Logger,
) -> None:
    path = tmp_path / "log" / "sanex.log"
    created = []
    chmod = log_module.os.chmod

    def create_handler(*args: object, **kwargs: object) -> WatchedFileHandler:
        handler = WatchedFileHandler(*args, **kwargs)
        created.append(handler)
        return handler

    def set_permissions(target: Path, mode: int) -> None:

        if target == path:
            raise PermissionError("file permissions denied")

        chmod(target, mode)

    original_handlers = tuple(app_logger.handlers)
    monkeypatch.setattr(log_module, "WatchedFileHandler", create_handler)
    monkeypatch.setattr(log_module.os, "chmod", set_permissions)

    with pytest.raises(StorageError, match="configure technical log.*file permissions denied"):
        configure_logging(path)

    assert len(created) == 1
    assert created[0].stream is None
    assert tuple(app_logger.handlers) == original_handlers


@pytest.mark.parametrize("fail", [False, True], ids=["normal", "exception"])
def test_context_closes_own_handlers_and_preserves_foreign_logging(
    tmp_path: Path, app_logger: logging.Logger, fail: bool,
) -> None:
    foreign = logging.NullHandler()
    app_logger.addHandler(foreign)
    app_logger.setLevel(logging.WARNING)
    app_logger.propagate = True
    original_handlers = tuple(app_logger.handlers)
    root = logging.getLogger()
    original_root = (tuple(root.handlers), root.level, root.propagate)
    owned = []
    error = RuntimeError("command failed")

    try:

        with logging_context(tmp_path / "log" / "sanex.log"):
            owned = [handler for handler in app_logger.handlers if handler not in original_handlers]
            assert len(owned) == 2
            assert foreign in app_logger.handlers
            assert app_logger.level == logging.INFO
            assert not app_logger.propagate

            if fail:
                raise error

    except RuntimeError as raised:
        assert fail
        assert raised is error

    assert tuple(app_logger.handlers) == original_handlers
    assert app_logger.level == logging.WARNING
    assert app_logger.propagate
    assert not foreign._closed
    assert all(handler._closed for handler in owned)
    assert (tuple(root.handlers), root.level, root.propagate) == original_root


def test_context_preserves_an_existing_technical_handler(tmp_path: Path, app_logger: logging.Logger) -> None:
    existing = configure_logging(tmp_path / "existing" / "sanex.log")
    foreign = logging.NullHandler()
    app_logger.addHandler(foreign)
    original_handlers = tuple(app_logger.handlers)

    with logging_context(tmp_path / "command" / "sanex.log"):
        assert existing not in app_logger.handlers

    assert tuple(app_logger.handlers) == original_handlers
    assert existing.stream is not None
    assert not existing._closed


def test_closes_prepared_handler_when_replacing_previous_handler_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_logger: logging.Logger,
) -> None:
    existing = configure_logging(tmp_path / "existing" / "sanex.log")
    original_close = existing.close
    created = []

    def close_existing() -> None:
        original_close()
        raise RuntimeError("previous handler close failed")

    def create_handler(*args: object, **kwargs: object) -> WatchedFileHandler:
        handler = WatchedFileHandler(*args, **kwargs)
        created.append(handler)
        return handler

    monkeypatch.setattr(existing, "close", close_existing)
    monkeypatch.setattr(log_module, "WatchedFileHandler", create_handler)

    with pytest.raises(RuntimeError, match="previous handler close failed"):
        configure_logging(tmp_path / "command" / "sanex.log")

    assert len(created) == 1
    assert created[0].stream is None
    assert created[0] not in app_logger.handlers


@pytest.mark.parametrize("primary_failure", [False, True], ids=["cleanup-error", "primary-error"])
def test_context_cleans_all_handlers_and_restores_logging_when_closing_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_logger: logging.Logger,
    capsys: pytest.CaptureFixture[str], primary_failure: bool,
) -> None:
    foreign = logging.NullHandler()
    app_logger.addHandler(foreign)
    app_logger.setLevel(logging.WARNING)
    app_logger.propagate = True
    original_handlers = tuple(app_logger.handlers)
    owned = []
    expected_error = ValueError if primary_failure else RuntimeError
    expected_text = "command failed" if primary_failure else "handler close failed"

    with pytest.raises(expected_error, match=expected_text):

        with logging_context(tmp_path / "log" / "sanex.log"):
            owned = [handler for handler in app_logger.handlers if handler not in original_handlers]
            file_handler = next(handler for handler in owned if isinstance(handler, WatchedFileHandler))
            original_close = file_handler.close

            def close_file_handler() -> None:
                original_close()
                raise RuntimeError("handler close failed")

            monkeypatch.setattr(file_handler, "close", close_file_handler)

            if primary_failure:
                raise ValueError("command failed")

    assert tuple(app_logger.handlers) == original_handlers
    assert app_logger.level == logging.WARNING
    assert app_logger.propagate
    assert not foreign._closed
    assert all(handler._closed for handler in owned)
    assert "Unable to close CLI log handler" in capsys.readouterr().err
