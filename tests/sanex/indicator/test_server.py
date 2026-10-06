"""Public read-only server behavior with simulated listener and peer credentials."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from sanex.indicator.server import IndicatorStatusServer
from sanex.model.indicator import IndicatorStatus
from tests.sanex.resource_fakes import FakeSocket, FakeWriter


@pytest.mark.asyncio
async def test_publishes_only_changed_status_to_peer_session(tmp_path: Path, unix_listener: SimpleNamespace) -> None:
    authorized = asyncio.Event()
    peer_uids = []
    current = [IndicatorStatus(remaining=3600, break_duration=7200)]

    def authorize(uid: int, sess_ident: str) -> bool:
        peer_uids.append(uid)
        authorized.set()
        return sess_ident == "3"

    server = IndicatorStatusServer(
        path=tmp_path / "run" / "indicator.sock", mode=0o600, authorize=authorize,
        provide_status=lambda uid, sess_ident, timestamp: current[0],
    )
    await server.start()
    reader = asyncio.StreamReader()
    reader.feed_data(b"{\"sess_ident\":\"3\"}\n")
    writer = FakeWriter(FakeSocket(uid=1001))
    previous_tasks = asyncio.all_tasks()
    task = asyncio.create_task(unix_listener.handler(reader, writer))
    await asyncio.wait_for(authorized.wait(), 1)

    server.publish(1790956800)
    await asyncio.wait_for(writer.drained.wait(), 1)
    current[0] = IndicatorStatus(remaining=3599, break_duration=7200)
    server.publish(1790956801)
    await asyncio.sleep(0)

    assert writer.writes == [IndicatorStatus(remaining=3600, break_duration=7200).encode()]
    assert peer_uids == [1001]
    writer.drained.clear()
    current[0] = IndicatorStatus(remaining=3540, break_duration=7200)
    server.publish(1790956860)
    await asyncio.wait_for(writer.drained.wait(), 1)
    assert writer.writes[-1] == current[0].encode()

    await server.close()
    await asyncio.gather(task, return_exceptions=True)

    assert writer.closed
    assert unix_listener.closed and unix_listener.waited
    assert unix_listener.permissions == [(server.path, 0o600)]
    assert asyncio.all_tasks() == previous_tasks


@pytest.mark.parametrize("payload", [b"{\"sess_ident\":\"foreign\"}\n", b"{\"uid\":1002,\"sess_ident\":\"3\"}\n", b"not-json\n"])
@pytest.mark.asyncio
async def test_rejects_foreign_session_and_client_supplied_identity(
    tmp_path: Path, unix_listener: SimpleNamespace, payload: bytes,
) -> None:
    server = IndicatorStatusServer(
        path=tmp_path / "indicator.sock", authorize=lambda uid, sess_ident: False,
        provide_status=lambda uid, sess_ident, timestamp: None,
    )
    await server.start()
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    writer = FakeWriter()

    await unix_listener.handler(reader, writer)

    assert writer.writes == []
    assert writer.closed
    await server.close()
