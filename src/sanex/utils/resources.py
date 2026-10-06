"""Complete resource cleanup without replacing an active failure or cancellation."""

import logging
import sys
from collections.abc import Awaitable, Callable
from inspect import isawaitable


logger = logging.getLogger(__name__)
Cleanup = tuple[str, Callable[[], None]]
AsyncCleanup = tuple[str, Callable[[], Awaitable[object] | None]]


def close_resources(*operations: Cleanup) -> None:
    """Try every release operation and report secondary failures."""
    primary = sys.exception()
    first_error = None

    for name, operation in operations:

        try:
            operation()

        except BaseException as error:
            logger.exception("Unable to %s", name)

            if first_error is None:
                first_error = error

    if first_error is not None and primary is None:
        raise first_error


async def close_async_resources(*operations: AsyncCleanup) -> None:
    """Release synchronous and asynchronous resources in the supplied order."""
    primary = sys.exception()
    first_error = None

    for name, operation in operations:

        try:
            result = operation()

            if isawaitable(result):
                await result

        except BaseException as error:
            logger.exception("Unable to %s", name)

            if first_error is None:
                first_error = error

    if first_error is not None and primary is None:
        raise first_error
