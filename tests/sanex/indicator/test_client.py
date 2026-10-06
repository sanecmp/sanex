"""Indicator notifications and retry scheduling with resource-only stream doubles."""

import asyncio
from collections.abc import Awaitable
from unittest.mock import AsyncMock

import pytest

from sanex.indicator import client as client_module
from sanex.indicator.client import IndicatorStatusClient, RECONNECT_DELAYS
from sanex.model.indicator import IndicatorStatus
from tests.sanex.resource_fakes import FakeWriter


def status_reader(status: IndicatorStatus) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(status.encode())
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_reports_unavailable_then_receives_status_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    status = IndicatorStatus(remaining=3600, break_duration=7200)
    writer = FakeWriter()
    connection = AsyncMock(side_effect=[OSError("not started"), (status_reader(status), writer)])
    monkeypatch.setattr(client_module.asyncio, "open_unix_connection", connection)
    stop_requested = asyncio.Event()
    received = []
    delays = []

    def receive(value: IndicatorStatus | None) -> None:
        received.append(value)

        if value is not None:
            stop_requested.set()

    async def wait_for_timeout(operation: Awaitable[object], timeout: float) -> None:
        delays.append(timeout)
        operation.close()
        raise TimeoutError

    monkeypatch.setattr(client_module, "wait_for_timeout", wait_for_timeout)

    await IndicatorStatusClient("3").run(stop_requested, receive)

    assert received == [None, status]
    assert delays == [RECONNECT_DELAYS[0]]
    assert connection.await_count == 2
    assert writer.writes == [b"{\"sess_ident\":\"3\"}\n"]
    assert writer.closed and writer.waited


@pytest.mark.asyncio
async def test_reports_disconnect_once_and_resets_reconnect_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    status = IndicatorStatus(remaining=None, break_duration=0)
    writers = [FakeWriter(), FakeWriter()]
    connection = AsyncMock(
        side_effect=[(status_reader(status), writers[0]), OSError("offline"), (status_reader(status), writers[1])],
    )
    monkeypatch.setattr(client_module.asyncio, "open_unix_connection", connection)
    stop_requested = asyncio.Event()
    received = []
    delays = []

    def receive(value: IndicatorStatus | None) -> None:
        received.append(value)

        if received == [None, status, None, status]:
            stop_requested.set()

    async def wait_for_timeout(operation: Awaitable[object], timeout: float) -> None:
        delays.append(timeout)
        operation.close()
        raise TimeoutError

    monkeypatch.setattr(client_module, "wait_for_timeout", wait_for_timeout)

    await IndicatorStatusClient("3").run(stop_requested, receive)

    assert received == [None, status, None, status]
    assert delays == [1, 2]
    assert all(writer.closed and writer.waited for writer in writers)


@pytest.mark.asyncio
async def test_cancelled_client_reaps_read_task_and_closes_writer(monkeypatch: pytest.MonkeyPatch) -> None:

    reading = asyncio.Event()
    read_cancelled = asyncio.Event()

    class Reader:
        async def readline(self) -> bytes:
            reading.set()

            try:
                await asyncio.Event().wait()

            finally:
                read_cancelled.set()

            return b""

    writer = FakeWriter()
    monkeypatch.setattr(client_module.asyncio, "open_unix_connection", AsyncMock(return_value=(Reader(), writer)))
    statuses = []
    client = IndicatorStatusClient("3")
    previous_tasks = asyncio.all_tasks()
    task = asyncio.create_task(client.run(asyncio.Event(), statuses.append))
    await asyncio.wait_for(reading.wait(), 1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert read_cancelled.is_set()
    assert writer.closed and writer.waited
    assert statuses == [None]
    assert asyncio.all_tasks() == previous_tasks
