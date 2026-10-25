"""Read only local display hints from a simulated user-manager bus."""

import pytest
from dbus_fast import Variant

from sanex.exceptions import WindowAgentError
from sanex.platform import session_environment
from .fakes import FakeBus, dbus_reply


@pytest.mark.asyncio
async def test_display_hints_are_whitelisted_and_connection_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus([dbus_reply([Variant("as", ["DISPLAY=:1", "XAUTHORITY=/synthetic/auth", "SECRET=not-retained"])])])
    monkeypatch.setattr(session_environment, "create_addressed_bus", lambda address: bus)

    environment = await session_environment.read_session_environment("unix:path=/synthetic/bus")

    assert environment == {"DISPLAY": ":1", "XAUTHORITY": "/synthetic/auth"}
    assert bus.messages[0].body == ["org.freedesktop.systemd1.Manager", "Environment"]
    assert bus.disconnect_count == 1
    assert bus.disconnect_wait_count == 1


@pytest.mark.asyncio
async def test_environment_failure_does_not_reveal_values_and_closes_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus([OSError("synthetic-secret")])
    monkeypatch.setattr(session_environment, "create_addressed_bus", lambda address: bus)

    with pytest.raises(WindowAgentError, match="read display environment.*OSError") as failure:
        await session_environment.read_session_environment("unix:path=/synthetic/bus")

    assert "synthetic-secret" not in f"{failure.value}"
    assert bus.disconnect_count == 1


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_the_environment_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus([ValueError("synthetic invalid reply")], {"disconnect": OSError("synthetic close failure")})
    monkeypatch.setattr(session_environment, "create_addressed_bus", lambda address: bus)

    with pytest.raises(WindowAgentError, match="read display environment.*ValueError"):
        await session_environment.read_session_environment("unix:path=/synthetic/bus")

    assert bus.disconnect_count == 1
    assert bus.disconnect_wait_count == 1
