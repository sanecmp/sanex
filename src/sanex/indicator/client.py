"""Resource-light client for the local indicator status socket."""

import asyncio
from asyncio import wait_for as wait_for_timeout
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..exceptions import IndicatorError, IndicatorProtocolError
from ..utils.resources import close_async_resources
from ..model.indicator import (
    IndicatorRequest,
    IndicatorStatus,
    parse_indicator_status,
)
from .server import DEFAULT_INDICATOR_SOCKET_PATH, MAX_REQUEST_SIZE


RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)
StatusReceiver = Callable[[IndicatorStatus | None], None]


@dataclass(slots=True)
class IndicatorStatusClient:
    """Receive pushed status and reconnect with bounded exponential delays."""

    sess_ident: str
    path: Path = DEFAULT_INDICATOR_SOCKET_PATH
    reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS
    _status: IndicatorStatus | None = field(default=None, init=False, repr=False)
    _notified: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:

        if not isinstance(self.sess_ident, str) or not self.sess_ident:
            raise IndicatorError("create client", "session ident is invalid")

        delays = self.reconnect_delays

        if not delays or any(
            not isinstance(delay, int | float)
            or isinstance(delay, bool)
            or delay <= 0
            for delay in delays
        ):
            raise IndicatorError("create client", "reconnect delays must be positive")

    async def run(
        self,
        stop_requested: asyncio.Event,
        receive: StatusReceiver,
    ) -> None:
        """Receive statuses until stopped, reporting disconnections once."""
        self._notified = False
        self._notify(receive, None)
        delays = self.reconnect_delays
        attempt = 0

        while not stop_requested.is_set():
            writer: asyncio.StreamWriter | None = None
            received = False
            try:
                reader, writer = await asyncio.open_unix_connection(
                    path=self.path,
                    limit=MAX_REQUEST_SIZE + 1,
                )
                request = IndicatorRequest(sess_ident=self.sess_ident)
                writer.write(request.encode())
                await writer.drain()
                consumed = await self._consume(reader, stop_requested, receive)

                if consumed is None:
                    return

                received = consumed

            except (IndicatorProtocolError, OSError):
                pass

            finally:

                if writer is not None:
                    await close_async_resources(("close indicator stream", lambda: self._close_writer(writer)))

            self._notify(receive, None)

            if received:
                attempt = 0

            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1

            if await self._wait_for_stop(stop_requested, delay):
                return

    async def _consume(
        self,
        reader: asyncio.StreamReader,
        stop_requested: asyncio.Event,
        receive: StatusReceiver,
    ) -> bool | None:
        received = False
        stop_task = asyncio.create_task(stop_requested.wait())
        read_task = None
        try:

            while True:
                read_task = asyncio.create_task(reader.readline())
                done, pending_tasks = await asyncio.wait(
                    (read_task, stop_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if stop_task in done:
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    return None

                try:
                    data = read_task.result()

                except ValueError as error:
                    raise IndicatorProtocolError(
                        "$",
                        "status message exceeds the size limit",
                    ) from error

                if not data:
                    return received

                if len(data) > MAX_REQUEST_SIZE or not data.endswith(b"\n"):
                    raise IndicatorProtocolError("$", "invalid status message frame")

                status = parse_indicator_status(data)
                self._notify(receive, status)
                received = True

        finally:
            tasks = (stop_task,) if read_task is None else (stop_task, read_task)

            for task in tasks:

                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

    def _notify(
        self,
        receive: StatusReceiver,
        status: IndicatorStatus | None,
    ) -> None:
        notified = self._notified

        if notified and status == self._status:
            return

        self._status = status
        self._notified = True
        receive(status)

    @staticmethod
    async def _wait_for_stop(
        stop_requested: asyncio.Event,
        delay: float,
    ) -> bool:
        try:
            await wait_for_timeout(stop_requested.wait(), timeout=delay)

        except asyncio.TimeoutError:
            return False

        return True

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()

        except (ConnectionError, OSError):
            pass
