"""Tests for non-overlapping synchronization scheduling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sanex.exceptions import SyncTransportError
from sanex.sync.scheduler import SyncScheduler


@pytest.mark.asyncio
async def test_run_once_runs_one_complete_exchange() -> None:
    exchange = SimpleNamespace(run=AsyncMock(return_value=True))
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    scheduler = SyncScheduler(lambda: config, exchange)

    assert await scheduler.run_once()
    exchange.run.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_scheduler_uses_success_and_retry_intervals_and_stops() -> None:
    exchange = SimpleNamespace(run=AsyncMock(side_effect=(False, True)))
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    delays: list[float] = []

    async def waiter(stop_requested: asyncio.Event, delay: float) -> bool:
        delays.append(delay)

        if len(delays) == 2:
            stop_requested.set()
            return True

        return False

    scheduler = SyncScheduler(lambda: config, exchange, waiter)

    await scheduler.run(asyncio.Event())

    assert delays == [30, 300]
    assert exchange.run.await_count == 2


@pytest.mark.asyncio
async def test_scheduler_limits_delays_when_requested() -> None:
    exchange = SimpleNamespace(run=AsyncMock(side_effect=(False, True)))
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    delays: list[float] = []

    async def waiter(stop_requested: asyncio.Event, delay: float) -> bool:
        delays.append(delay)
        return len(delays) == 2

    scheduler = SyncScheduler(
        lambda: config,
        exchange,
        waiter,
        delay_limit=2,
    )

    await scheduler.run(asyncio.Event())

    assert delays == [2, 2]


@pytest.mark.asyncio
async def test_failed_exchange_is_retried_without_stopping_scheduler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exchange = SimpleNamespace(
        run=AsyncMock(
            side_effect=(SyncTransportError("sync", OSError("offline")), True)
        ),
    )
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    delays: list[float] = []

    async def waiter(stop_requested: asyncio.Event, delay: float) -> bool:
        delays.append(delay)
        return len(delays) == 2

    scheduler = SyncScheduler(lambda: config, exchange, waiter)

    with caplog.at_level("ERROR"):
        await scheduler.run(asyncio.Event())

    assert delays == [30, 300]
    assert "Synchronization attempt failed" in caplog.text


@pytest.mark.asyncio
async def test_unexpected_exchange_failure_is_not_suppressed() -> None:
    exchange = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("bug")))
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    scheduler = SyncScheduler(lambda: config, exchange)

    with pytest.raises(RuntimeError, match="bug"):
        await scheduler.run(asyncio.Event())
