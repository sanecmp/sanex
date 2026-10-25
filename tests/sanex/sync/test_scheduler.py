"""Tests for non-overlapping synchronization scheduling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sanex.exceptions import SyncTransportError
from sanex.sync.protocol import parse_sync_response
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
            side_effect=(SyncTransportError("sync", OSError("IDLE_TEST_SYNTHETIC_SECRET")), True)
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
    assert "Synchronization attempt failed (SyncTransportError)" in caplog.text
    assert "IDLE_TEST_SYNTHETIC_SECRET" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_invalid_sync_response_is_retried_without_logging_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exchange_response() -> bool:
        parse_sync_response(
            b"{\"config\":null,\"commands\":[],\"audit_secret\":\"IDLE_TEST_SYNTHETIC_SECRET\"}"
        )
        return True

    exchange = SimpleNamespace(run=exchange_response)
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    waiter = AsyncMock(return_value=True)
    stop_requested = asyncio.Event()
    scheduler = SyncScheduler(lambda: config, exchange, waiter)

    with caplog.at_level("ERROR"):
        await scheduler.run(stop_requested)

    waiter.assert_awaited_once_with(stop_requested, 30)
    assert "Synchronization attempt failed (SyncProtocolError)" in caplog.text
    assert "IDLE_TEST_SYNTHETIC_SECRET" not in caplog.text
    assert "input_value" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_unexpected_exchange_failure_is_not_suppressed() -> None:
    exchange = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("bug")))
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    scheduler = SyncScheduler(lambda: config, exchange)

    with pytest.raises(RuntimeError, match="bug"):
        await scheduler.run(asyncio.Event())


@pytest.mark.asyncio
async def test_connection_changes_are_logged_without_repeating_unchanged_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = SyncTransportError("sync", OSError("synthetic secret"))
    outcomes = [False, False, True, True, failure, failure, False, False, True]
    exchange = SimpleNamespace(run=AsyncMock(side_effect=outcomes))
    config = SimpleNamespace(sync_interval=300, discovery_interval=30)
    delays = []

    async def waiter(stop_requested: asyncio.Event, delay: float) -> bool:
        delays.append(delay)
        return len(delays) == len(outcomes)

    scheduler = SyncScheduler(lambda: config, exchange, waiter)

    with caplog.at_level("INFO"):
        await scheduler.run(asyncio.Event())

    assert delays == [30, 30, 300, 300, 30, 30, 30, 30, 300]
    assert caplog.messages == [
        "Sanea was not discovered; using saved configuration",
        "Sanea connection restored",
        "Synchronization completed",
        "Synchronization completed",
        "Synchronization attempt failed (SyncTransportError)",
        "Sanea was not discovered; using saved configuration",
        "Sanea connection restored",
        "Synchronization completed",
    ]
