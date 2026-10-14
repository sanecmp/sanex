"""Tests for systemd-logind session snapshots and termination."""

from typing import Any

import pytest
from dbus_fast import Message, Variant

from sanex.exceptions import LogindError
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.platform.logind import LogindClient
from tests.sanex.platform.fakes import FakeBus, dbus_error, dbus_reply

import asyncio


def session_properties(sample: dict[str, Any]) -> dict[str, Variant]:
    """Build logind session properties from a datafixture entry."""
    return {
        "Id": Variant("s", sample["ident"]),
        "User": Variant("(uo)", (sample["uid"], sample["user_path"])),
        "Name": Variant("s", sample["login"]),
        "Timestamp": Variant("t", sample["timestamp_us"]),
        "Type": Variant("s", sample["type"]),
        "Class": Variant("s", sample["class"]),
        "Active": Variant("b", sample["active"]),
        "State": Variant("s", sample["state"]),
        "IdleHint": Variant("b", sample["idle"]),
        "LockedHint": Variant("b", sample["locked"]),
    }


def session_row(sample: dict[str, Any]) -> list[object]:
    """Build one ListSessions row from a datafixture entry."""
    return [sample["ident"], sample["uid"], sample["login"], sample["seat"], sample["path"]]


@pytest.mark.asyncio
async def test_snapshot_keeps_normal_graphical_sessions(
    session_payload: list[dict[str, Any]],
) -> None:
    rows = [session_row(sample) for sample in session_payload]
    bus = FakeBus(
        [
            dbus_reply([rows]),
            *(dbus_reply([session_properties(sample)]) for sample in session_payload),
        ]
    )
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()

    sessions = await client.read_sessions()

    assert sessions == (
        LoginSession(
            ident="2",
            uid=1001,
            login="child",
            path="/org/freedesktop/login1/session/_32",
            started=1790910138,
            type=SessionType.WAYLAND,
            state=SessionState.ACTIVE,
            active=True,
            idle=False,
            locked=False,
        ),
        LoginSession(
            ident="3",
            uid=1002,
            login="second-child",
            path="/org/freedesktop/login1/session/_33",
            started=1790910200,
            type=SessionType.X11,
            state=SessionState.ONLINE,
            active=False,
            idle=True,
            locked=True,
        ),
    )
    assert [message.member for message in bus.messages] == ["ListSessions", "GetAll", "GetAll", "GetAll", "GetAll"]
    client.close()
    assert not bus.connected


@pytest.mark.asyncio
async def test_terminate_targets_exactly_one_session() -> None:
    bus = FakeBus([dbus_reply([])])
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()

    await client.terminate("2")

    message = bus.messages[0]
    assert message.member == "TerminateSession"
    assert message.signature == "s"
    assert message.body == ["2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [None, 1001])
async def test_finds_session_containing_process(uid: int | None) -> None:
    bus = FakeBus(
        [
            dbus_reply(["/org/freedesktop/login1/session/_32"]),
            dbus_reply([Variant("s", "2")]),
        ]
    )
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()

    ident = await client.get_session_ident(1234, uid=uid)

    assert ident == "2"
    assert [message.member for message in bus.messages] == [
        "GetSessionByPID",
        "Get",
    ]
    assert bus.messages[0].body == [1234]


@pytest.mark.asyncio
@pytest.mark.parametrize(("uid", "expected"), [(1001, "2"), (1002, "3")])
async def test_finds_own_graphical_session_for_process_outside_session(
    session_payload: list[dict[str, Any]], uid: int, expected: str,
) -> None:
    bus = FakeBus([
        dbus_error("org.freedesktop.login1.NoSessionForPID", "PID does not belong to any known session"),
        dbus_reply([[session_row(sample) for sample in session_payload]]),
        *(dbus_reply([session_properties(sample)]) for sample in session_payload),
    ])
    client = LogindClient(bus_factory=lambda: bus)

    async with client:
        ident = await client.get_session_ident(1234, uid=uid)

    assert ident == expected
    assert [message.member for message in bus.messages] == ["GetSessionByPID", "ListSessions", *(["GetAll"] * 4)]
    assert not bus.connected


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "closing"])
async def test_process_session_lookup_rejects_ambiguous_or_closing_sessions(
    session_payload: list[dict[str, Any]], state: str,
) -> None:
    session_payload[0]["state"] = state
    session_payload[1]["uid"] = 1001
    session_payload[1]["state"] = state
    bus = FakeBus([
        dbus_error("org.freedesktop.login1.NoSessionForPID", "PID does not belong to any known session"),
        dbus_reply([[session_row(sample) for sample in session_payload]]),
        *(dbus_reply([session_properties(sample)]) for sample in session_payload),
    ])
    client = LogindClient(bus_factory=lambda: bus)

    async with client:

        with pytest.raises(LogindError, match="No unique graphical session for UID 1001"):
            await client.get_session_ident(1234, uid=1001)

    assert not bus.connected


@pytest.mark.asyncio
async def test_process_session_lookup_does_not_choose_another_user(
    session_payload: list[dict[str, Any]],
) -> None:
    bus = FakeBus([
        dbus_error("org.freedesktop.login1.NoSessionForPID", "PID does not belong to any known session"),
        dbus_reply([[session_row(sample) for sample in session_payload]]),
        *(dbus_reply([session_properties(sample)]) for sample in session_payload),
    ])
    client = LogindClient(bus_factory=lambda: bus)

    async with client:

        with pytest.raises(LogindError, match="No unique graphical session for UID 9999"):
            await client.get_session_ident(1234, uid=9999)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "error_name"),
    [(None, "org.freedesktop.login1.NoSessionForPID"), (1001, "org.freedesktop.DBus.Error.AccessDenied")],
)
async def test_process_session_lookup_preserves_unrecoverable_errors(uid: int | None, error_name: str) -> None:
    bus = FakeBus([dbus_error(error_name, "lookup failed")])
    client = LogindClient(bus_factory=lambda: bus)

    async with client:

        with pytest.raises(LogindError, match="lookup failed"):
            await client.get_session_ident(1234, uid=uid)

    assert [message.member for message in bus.messages] == ["GetSessionByPID"]


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [True, -1, "1001"])
async def test_process_session_lookup_rejects_invalid_uid(uid: object) -> None:
    bus = FakeBus([])
    client = LogindClient(bus_factory=lambda: bus)

    async with client:

        with pytest.raises(LogindError, match="UID is invalid"):
            await client.get_session_ident(1234, uid=uid)

    assert not bus.messages


@pytest.mark.asyncio
async def test_inconsistent_session_properties_fail_the_whole_snapshot(
    session_payload: list[dict[str, Any]],
) -> None:
    sample = session_payload[0]
    properties = session_properties(sample)
    properties["Name"] = Variant("s", "other")
    bus = FakeBus([dbus_reply([[session_row(sample)]]), dbus_reply([properties])])
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()

    with pytest.raises(LogindError, match="do not match ListSessions"):
        await client.read_sessions()


@pytest.mark.asyncio
async def test_client_requires_an_explicit_connection() -> None:
    client = LogindClient(bus_factory=lambda: FakeBus([]))

    with pytest.raises(LogindError, match="client is not connected"):
        await client.read_sessions()


@pytest.mark.asyncio
async def test_sleep_monitoring_reports_suspend_and_resume() -> None:
    bus = FakeBus([dbus_reply([]), dbus_reply([])])
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()

    await client.start_sleep_monitoring()
    bus.emit(
        Message.new_signal(
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            "b",
            [True],
        )
    )
    bus.emit(
        Message.new_signal(
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            "b",
            [False],
        )
    )

    assert await client.wait_for_sleep_change() is True
    assert await client.wait_for_sleep_change() is False
    add_match = bus.messages[0]
    assert add_match.member == "AddMatch"
    assert add_match.signature == "s"
    assert "member='PrepareForSleep'" in add_match.body[0]

    await client.stop_sleep_monitoring()

    assert bus.messages[1].member == "RemoveMatch"
    assert not bus.handlers

    with pytest.raises(LogindError, match="sleep monitoring is not active"):
        await client.wait_for_sleep_change()


@pytest.mark.asyncio
async def test_closing_client_removes_local_sleep_handler() -> None:
    bus = FakeBus([dbus_reply([])])
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()
    await client.start_sleep_monitoring()

    client.close()

    assert not bus.connected
    assert not bus.handlers


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_failed_connection_disconnects_and_remains_unconnected(cancel: bool) -> None:

    failure = asyncio.CancelledError("connect interrupted") if cancel else OSError("connect interrupted")
    bus = FakeBus([], {"connect": failure})
    client = LogindClient(bus_factory=lambda: bus)
    expected = asyncio.CancelledError if cancel else LogindError

    with pytest.raises(expected, match="connect interrupted"):
        await client.connect()

    assert bus.disconnect_count == 1

    with pytest.raises(LogindError, match="client is not connected"):
        await client.read_sessions()


@pytest.mark.parametrize("operation", ["add_handler", "AddMatch"])
@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_failed_subscription_removes_local_handler(operation: str, cancel: bool) -> None:

    failure = asyncio.CancelledError("subscription interrupted") if cancel else OSError("subscription interrupted")
    bus = FakeBus([], {operation: failure})
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()
    expected = asyncio.CancelledError if cancel else LogindError

    with pytest.raises(expected, match="subscription interrupted"):
        await client.start_sleep_monitoring()

    assert bus.handlers == []

    with pytest.raises(LogindError, match="sleep monitoring is not active"):
        await client.wait_for_sleep_change()

    client.close()
    assert bus.disconnect_count == 1


@pytest.mark.asyncio
async def test_close_disconnects_even_when_handler_removal_fails() -> None:
    bus = FakeBus([dbus_reply([])])
    client = LogindClient(bus_factory=lambda: bus)
    await client.connect()
    await client.start_sleep_monitoring()
    bus.failures["remove_handler"] = OSError("handler removal denied")

    with pytest.raises(LogindError, match="handler removal denied"):
        client.close()

    assert bus.disconnect_count == 1
    client.close()
    assert bus.disconnect_count == 1
