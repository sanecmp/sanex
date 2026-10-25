"""Combining independent window sources and routing their actions."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sanelib.protocol import ObservationSource

from sanex.exceptions import AtspiError, X11Error
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.desktop_windows import DesktopWindowClient
from sanex.platform.x11 import X11Client


@pytest.mark.asyncio
async def test_accessibility_stays_primary_but_ewmh_supplies_inaccessible_games() -> None:
    accessible = AtspiWindow(bus=":1.2", path="/window", pid=100, title="Synthetic Steam", role=WindowRole.FRAME)
    duplicate = accessible.model_copy(update={"source": ObservationSource.EWMH, "bus": "ewmh", "path": "10"})
    game = duplicate.model_copy(update={"path": "11", "pid": 200, "title": "Team Fortress 2"})
    accessibility = SimpleNamespace(connect=AsyncMock(), read_windows=AsyncMock(return_value=AtspiWindowSnapshot(windows=(accessible,))),
                                    close_window=AsyncMock(return_value=True), close=AsyncMock())
    x11 = SimpleNamespace(connect=Mock(), read_windows=Mock(return_value=AtspiWindowSnapshot(windows=(duplicate, game))),
                         close_window=Mock(return_value=True), close=Mock())
    client = DesktopWindowClient(accessibility, x11)
    await client.connect({}, Path("/synthetic/home"))

    snapshot = await client.read_windows()
    assert snapshot.windows == (accessible, game)
    assert await client.close_window(game)
    assert await client.close_window(accessible)
    x11.close_window.assert_called_once_with(game)
    accessibility.close_window.assert_awaited_once_with(accessible)

    accessibility.read_windows.side_effect = AtspiError("snapshot", OSError("unavailable"))
    incomplete = await client.read_windows()
    assert incomplete.windows == (duplicate, game)
    assert incomplete.unavailable_sources == (ObservationSource.ATSPI,)
    assert not incomplete.complete
    await client.close()
    accessibility.close.assert_awaited_once()
    x11.close.assert_called_once()


@pytest.mark.asyncio
async def test_x11_failure_preserves_accessibility_and_marks_only_ewmh_unavailable() -> None:
    window = AtspiWindow(bus=":1.2", path="/window", pid=100, title="Synthetic editor", role=WindowRole.FRAME)
    accessibility = SimpleNamespace(connect=AsyncMock(), read_windows=AsyncMock(return_value=AtspiWindowSnapshot(windows=(window,))),
                                    close=AsyncMock())
    x11 = SimpleNamespace(connect=Mock(), read_windows=Mock(side_effect=X11Error("lost display")), close=Mock())
    client = DesktopWindowClient(accessibility, x11)
    await client.connect({}, Path("/synthetic/home"))

    snapshot = await client.read_windows()

    assert snapshot.windows == (window,)
    assert snapshot.unavailable_sources == (ObservationSource.EWMH,)
    assert not snapshot.complete
    await client.close()


@pytest.mark.asyncio
async def test_accessibility_is_reconnected_and_availability_changes_are_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    accessibility = SimpleNamespace(
        connect=AsyncMock(), close=AsyncMock(), read_windows=AsyncMock(return_value=AtspiWindowSnapshot()),
    )
    x11 = SimpleNamespace(connect=Mock(), close=Mock(), read_windows=Mock(return_value=AtspiWindowSnapshot()))
    client = DesktopWindowClient(accessibility, x11)
    failure = AtspiError("list applications", EOFError("synthetic secret"))

    with caplog.at_level("INFO"):
        await client.connect({}, Path("/synthetic/home"))
        accessibility.connect.side_effect = [failure, failure, None]
        assert (await client.read_windows()).unavailable_sources == (ObservationSource.ATSPI,)
        assert (await client.read_windows()).unavailable_sources == (ObservationSource.ATSPI,)
        assert (await client.read_windows()).complete
        await client.close()

    assert accessibility.connect.await_count == 4
    assert accessibility.read_windows.await_count == 1
    assert caplog.messages == [
        "Accessibility source is unavailable (EOFError); retrying",
        "Accessibility source restored",
    ]


@pytest.mark.asyncio
async def test_x11_connection_warning_includes_the_safe_failure_reason(caplog: pytest.LogCaptureFixture) -> None:
    accessibility = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    x11 = X11Client(display_factory=Mock(side_effect=PermissionError("synthetic secret")))
    client = DesktopWindowClient(accessibility, x11)

    await client.connect({"DISPLAY": ":1"}, Path("/synthetic/home"))
    await client.close()

    assert "X11 source is unavailable (X11 connection failed (PermissionError)); using accessibility only" in caplog.text
    assert "synthetic secret" not in caplog.text


@pytest.mark.asyncio
async def test_close_releases_accessibility_even_when_x11_cleanup_fails() -> None:
    accessibility = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    x11 = SimpleNamespace(connect=Mock(), close=Mock(side_effect=X11Error("synthetic close failure")))
    client = DesktopWindowClient(accessibility, x11)
    await client.connect({}, Path("/synthetic/home"))

    with pytest.raises(X11Error, match="synthetic close failure"):
        await client.close()

    accessibility.close.assert_awaited_once()
