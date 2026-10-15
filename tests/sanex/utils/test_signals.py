"""Stop handlers are optional and only installed handlers are released."""

import asyncio
import logging
import signal
from unittest.mock import Mock, call

import pytest

from sanex.utils.signals import install_stop_signals, remove_signal_handlers


@pytest.mark.parametrize("failure", [
    pytest.param(None, id="supported"),
    pytest.param(NotImplementedError(), id="unsupported"),
    pytest.param(RuntimeError("not the main thread"), id="unavailable"),
])
def test_stop_signals_release_only_successfully_installed_handlers(failure: Exception | None) -> None:
    loop = Mock(spec=asyncio.AbstractEventLoop)
    loop.add_signal_handler.side_effect = [failure, None]
    request_stop = Mock()

    installed = install_stop_signals(loop, request_stop)
    remove_signal_handlers(loop, installed)

    expected = (signal.SIGINT, signal.SIGTERM) if failure is None else (signal.SIGTERM,)
    assert installed == expected
    assert loop.add_signal_handler.call_args_list == [
        call(signal.SIGINT, request_stop), call(signal.SIGTERM, request_stop),
    ]
    assert loop.remove_signal_handler.call_args_list == [call(signum) for signum in expected]


def test_disabled_stop_signals_leave_the_loop_unchanged() -> None:
    loop = Mock(spec=asyncio.AbstractEventLoop)

    installed = install_stop_signals(loop, Mock(), enabled=False)
    remove_signal_handlers(loop, installed)

    assert installed == ()
    loop.add_signal_handler.assert_not_called()
    loop.remove_signal_handler.assert_not_called()


def test_stop_signals_report_unavailable_handlers_when_requested(caplog: pytest.LogCaptureFixture) -> None:
    loop = Mock(spec=asyncio.AbstractEventLoop)
    loop.add_signal_handler.side_effect = [RuntimeError("not the main thread"), None]

    install_stop_signals(loop, Mock(), warning_logger=logging.getLogger(__name__))

    assert "Unable to install handler for SIGINT" in caplog.text
    assert "SIGTERM" not in caplog.text
