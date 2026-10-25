"""D-Bus disconnect completion without real bus connections."""

import asyncio

import pytest

from sanex.platform.dbus import disconnect_bus
from .fakes import FakeBus


@pytest.mark.parametrize("failure", [None, EOFError("synthetic logout"), OSError("synthetic disconnect")])
@pytest.mark.asyncio
async def test_disconnect_retrieves_expected_completion(failure: Exception | None) -> None:
    bus = FakeBus([])
    await bus.connect()

    if failure is not None:
        bus.drop(failure)

    await disconnect_bus(bus)

    assert not bus.connected
    assert bus.disconnect_count == 1
    assert bus.disconnect_wait_count == 1


@pytest.mark.asyncio
async def test_disconnect_preserves_the_first_cleanup_failure_and_retrieves_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = FakeBus([], {"disconnect": OSError("synthetic close failure")})
    await bus.connect()
    bus.drop(RuntimeError("synthetic secondary failure"))

    with pytest.raises(OSError, match="synthetic close failure"):
        await disconnect_bus(bus)

    assert bus.disconnect_wait_count == 1
    assert "synthetic secondary failure" in caplog.text


@pytest.mark.asyncio
async def test_failed_setup_does_not_wait_for_a_connection_that_never_completed() -> None:
    bus = FakeBus([])

    await disconnect_bus(bus, wait=False)

    assert bus.disconnect_count == 1
    assert bus.disconnect_wait_count == 0


@pytest.mark.parametrize("failure", [RuntimeError("synthetic bug"), asyncio.CancelledError("synthetic cancellation")])
@pytest.mark.asyncio
async def test_disconnect_does_not_suppress_unexpected_failure_or_cancellation(failure: BaseException) -> None:
    bus = FakeBus([])
    await bus.connect()
    bus.drop(failure)

    with pytest.raises(type(failure), match="synthetic"):
        await disconnect_bus(bus)

    assert bus.disconnect_count == 1
    assert bus.disconnect_wait_count == 1
