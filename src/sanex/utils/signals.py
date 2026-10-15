"""Install and release event-loop handlers for orderly process shutdown."""

import asyncio
import logging
import signal
from collections.abc import Callable


def install_stop_signals(
    loop: asyncio.AbstractEventLoop,
    request_stop: Callable[[], None],
    *,
    enabled: bool = True,
    warning_logger: logging.Logger | None = None,
) -> tuple[signal.Signals, ...]:
    """Register supported stop handlers and return only installed signals."""

    if not enabled:
        return ()

    installed = []

    for signum in (signal.SIGINT, signal.SIGTERM):

        try:
            loop.add_signal_handler(signum, request_stop)

        except (NotImplementedError, RuntimeError):

            if warning_logger is not None:
                warning_logger.warning("Unable to install handler for %s", signum.name)

        else:
            installed.append(signum)

    return tuple(installed)


def remove_signal_handlers(loop: asyncio.AbstractEventLoop, installed: tuple[signal.Signals, ...]) -> None:
    """Remove exactly the handlers registered by the caller."""

    for signum in installed:
        loop.remove_signal_handler(signum)
