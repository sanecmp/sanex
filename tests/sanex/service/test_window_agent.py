"""Inherited-channel ownership and EUID lifecycle through the public agent runner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sanex.service import window_agent as agent_module
from sanex.platform.atspi import AtspiClient
from sanex.platform.desktop_windows import DesktopWindowClient
from tests.sanex.platform.fakes import FakeBus, dbus_reply
from tests.sanex.resource_fakes import FakeSocket, FakeWriter


@pytest_asyncio.fixture
async def agent_boundaries(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    channel = FakeSocket()
    writer = FakeWriter(channel)
    discovery = FakeBus([dbus_reply(["unix:path=/test/atspi"])])
    accessibility = FakeBus([])
    reader = asyncio.StreamReader()
    reader.feed_eof()
    privileges = []
    identity = SimpleNamespace(uid=0)

    def set_euid(uid: int) -> None:
        privileges.append(uid)
        identity.uid = uid

    dumping = []
    monkeypatch.setattr(agent_module.os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(agent_module.os, "geteuid", lambda: identity.uid)
    monkeypatch.setattr(agent_module.os, "seteuid", set_euid)
    libc = SimpleNamespace(prctl=lambda *args: dumping.append(args) or 0)
    monkeypatch.setattr(agent_module.ctypes, "CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr(agent_module, "socket", SimpleNamespace(socket=lambda **kwargs: channel))
    monkeypatch.setattr(agent_module.asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
    monkeypatch.setattr(agent_module, "create_addressed_bus", lambda address: discovery)
    monkeypatch.setattr(agent_module, "read_session_environment", AsyncMock(return_value={}))
    monkeypatch.setattr(agent_module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir="/synthetic/home"))
    monkeypatch.setattr(
        agent_module, "AtspiClient",
        lambda **kwargs: AtspiClient(atspi_bus_factory=lambda address: accessibility, **kwargs),
    )
    return SimpleNamespace(
        channel=channel, writer=writer, reader=reader, discovery=discovery,
        accessibility=accessibility, privileges=privileges, dumping=dumping, identity=identity,
    )


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_open_failure_closes_inherited_socket(
    monkeypatch: pytest.MonkeyPatch, agent_boundaries: SimpleNamespace, cancel: bool,
) -> None:
    failure = asyncio.CancelledError("channel interrupted") if cancel else OSError("channel interrupted")
    monkeypatch.setattr(agent_module.asyncio, "open_connection", AsyncMock(side_effect=failure))

    with pytest.raises(type(failure), match="channel interrupted"):
        await agent_module.run_agent(7, 1001, "3")

    assert agent_boundaries.channel.closed
    assert agent_boundaries.privileges == []


@pytest.mark.asyncio
async def test_connect_failure_restores_euid_and_closes_resources(agent_boundaries: SimpleNamespace) -> None:
    agent_boundaries.accessibility.failures["connect"] = RuntimeError("accessibility failed")

    with pytest.raises(RuntimeError, match="accessibility failed"):
        await agent_module.run_agent(7, 1001, "3")

    assert agent_boundaries.privileges == [1001, 0]
    assert agent_boundaries.accessibility.disconnect_count == 1
    assert agent_boundaries.writer.closed and agent_boundaries.writer.waited
    assert agent_boundaries.dumping == [(4, 0, 0, 0, 0)]


@pytest.mark.parametrize("primary", [False, True], ids=["restore-error", "secondary-restore-error"])
@pytest.mark.asyncio
async def test_restore_failure_is_reported_without_replacing_connect_failure(
    monkeypatch: pytest.MonkeyPatch, agent_boundaries: SimpleNamespace, caplog: pytest.LogCaptureFixture, primary: bool,
) -> None:
    def set_euid(uid: int) -> None:
        agent_boundaries.privileges.append(uid)

        if uid == 0:
            raise PermissionError("restore denied")

        agent_boundaries.identity.uid = uid

    if primary:
        agent_boundaries.discovery.failures["connect"] = RuntimeError("connect failed")

    monkeypatch.setattr(agent_module.os, "seteuid", set_euid)
    expected = RuntimeError if primary else PermissionError
    message = "connect failed" if primary else "restore denied"

    with pytest.raises(expected, match=message):
        await agent_module.run_agent(7, 1001, "3")

    assert agent_boundaries.writer.closed and agent_boundaries.writer.waited
    assert "restore denied" in caplog.text
    assert agent_boundaries.privileges == [1001, 0]


@pytest.mark.asyncio
async def test_reconnecting_inside_a_running_agent_uses_the_session_euid(
    monkeypatch: pytest.MonkeyPatch, agent_boundaries: SimpleNamespace,
) -> None:
    replacement = FakeBus([dbus_reply([[]])])
    connections = iter((agent_boundaries.accessibility, replacement))
    agent_boundaries.discovery.responses.append(dbus_reply(["unix:path=/test/atspi"]))
    agent_boundaries.accessibility.responses.append(dbus_reply([[]]))
    connection_uids = []

    def create_accessibility(address: str) -> FakeBus:
        connection_uids.append(agent_boundaries.identity.uid)
        return next(connections)

    def create_server(
        session: str, client: DesktopWindowClient, reader: asyncio.StreamReader, writer: FakeWriter,
    ) -> SimpleNamespace:
        async def serve() -> None:
            assert agent_boundaries.identity.uid == 0
            assert (await client.read_windows()).complete
            agent_boundaries.accessibility.drop(EOFError("synthetic disconnect"))
            assert (await client.read_windows()).complete
            assert agent_boundaries.identity.uid == 0

        return SimpleNamespace(serve=serve)

    monkeypatch.setattr(
        agent_module, "AtspiClient", lambda **kwargs: AtspiClient(atspi_bus_factory=create_accessibility, **kwargs),
    )
    monkeypatch.setattr(agent_module, "WindowAgentServer", create_server)

    await agent_module.run_agent(7, 1001, "3")

    assert connection_uids == [1001, 1001]
    assert agent_boundaries.privileges == [1001, 0, 1001, 0]
    assert agent_boundaries.accessibility.disconnect_wait_count == 1
    assert replacement.disconnect_wait_count == 1
    assert agent_boundaries.writer.closed and agent_boundaries.writer.waited
