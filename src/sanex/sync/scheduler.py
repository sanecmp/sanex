"""Scheduling of non-overlapping synchronization attempts."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sanelib.protocol import Config

from ..exceptions import SaneaException


logger = logging.getLogger(__name__)


class FullExchange(Protocol):
    """Prepare and run one complete discovery-led synchronization cycle."""

    async def run(self) -> bool: ...


async def _wait_for_stop(stop_requested: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_requested.wait(), delay)

    except TimeoutError:
        return False

    return True


@dataclass(slots=True)
class SyncScheduler:
    """Serially schedule complete synchronization attempts."""

    config: Callable[[], Config]
    exchange: FullExchange
    waiter: Callable[[asyncio.Event, float], Awaitable[bool]] = _wait_for_stop
    delay_limit: float | None = None

    async def run_once(self) -> bool:
        """Run one complete exchange attempt without overlap."""
        return await self.exchange.run()

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Attempt immediately, then wait according to the latest config."""

        while not stop_requested.is_set():
            try:
                successful = await self.run_once()

            except SaneaException:
                logger.exception("Synchronization attempt failed")
                successful = False

            if successful:
                logger.info("Synchronization completed")

            current_config = self.config()
            delay = (
                current_config.sync_interval
                if successful
                else current_config.discovery_interval
            )
            delay_limit = self.delay_limit

            if delay_limit is not None:
                delay = min(delay, delay_limit)

            if await self.waiter(stop_requested, delay):
                break
