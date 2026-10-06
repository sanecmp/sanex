"""Discovery framing and deadlines with isolated UDP and monotonic boundaries."""

from collections import deque
from collections.abc import Awaitable
from types import SimpleNamespace

import pytest

from sanex.exceptions import SaneaDiscoveryError
from sanex.sync import discovery as discovery_module
from sanex.sync.discovery import DISCOVERY_TIMEOUT, DiscoveryMode, SaneaDiscovery
from tests.sanex.resource_fakes import FakeSocket


class FakeLoop:
    def __init__(self, responses: tuple[bytes, ...]) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[bytes, tuple[str, int]]] = []
        self.send_failure: OSError | None = None

    def time(self) -> float:
        return 1000.0

    async def sock_sendto(self, channel: FakeSocket, data: bytes, destination: tuple[str, int]) -> None:
        self.requests.append((data, destination))

        if self.send_failure is not None:
            raise self.send_failure

    async def sock_recvfrom(self, channel: FakeSocket, size: int) -> tuple[bytes, tuple[str, int]]:

        if not self.responses:
            raise TimeoutError

        return self.responses.popleft(), ("127.0.0.1", 62117)


@pytest.fixture
def udp_boundaries(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    channel = FakeSocket()
    original_socket = discovery_module.socket
    monkeypatch.setattr(discovery_module, "socket", SimpleNamespace(
        socket=lambda *args: channel, AF_INET=original_socket.AF_INET, SOCK_DGRAM=original_socket.SOCK_DGRAM,
        SOL_SOCKET=original_socket.SOL_SOCKET, SO_BROADCAST=original_socket.SO_BROADCAST,
    ))
    deadlines = []

    async def wait_for_timeout(operation: Awaitable[object], timeout: float) -> object:
        deadlines.append(timeout)
        return await operation

    monkeypatch.setattr(discovery_module, "wait_for_timeout", wait_for_timeout)
    return SimpleNamespace(channel=channel, deadlines=deadlines)


@pytest.mark.parametrize("mode", [DiscoveryMode.SYNC, DiscoveryMode.REGISTRATION])
@pytest.mark.asyncio
async def test_discovers_first_valid_response(
    monkeypatch: pytest.MonkeyPatch, udp_boundaries: SimpleNamespace, mode: DiscoveryMode,
) -> None:
    loop = FakeLoop((b"not-json", b"{\"port\":8443}"))
    monkeypatch.setattr(discovery_module, "current_loop", lambda: loop)

    endpoint = await SaneaDiscovery().discover(mode)

    assert endpoint is not None
    assert endpoint.base_url == "https://127.0.0.1:8443"
    assert loop.requests == [(mode.value.encode("ascii"), (target, 62117)) for target in ("255.255.255.255", "127.0.0.1")]
    assert udp_boundaries.deadlines == [DISCOVERY_TIMEOUT, DISCOVERY_TIMEOUT]
    assert udp_boundaries.channel.address == ("", 0)
    assert udp_boundaries.channel.closed


@pytest.mark.asyncio
async def test_ignores_invalid_responses_until_resource_timeout(
    monkeypatch: pytest.MonkeyPatch, udp_boundaries: SimpleNamespace,
) -> None:
    loop = FakeLoop((b"{\"port\":true}", b"{\"port\":0}", b"{\"port\":8443,\"extra\":1}", b"a" * 257))
    monkeypatch.setattr(discovery_module, "current_loop", lambda: loop)

    assert await SaneaDiscovery().discover() is None

    assert udp_boundaries.deadlines == [DISCOVERY_TIMEOUT] * 5
    assert udp_boundaries.channel.closed


@pytest.mark.asyncio
async def test_failed_destinations_close_udp_socket(
    monkeypatch: pytest.MonkeyPatch, udp_boundaries: SimpleNamespace,
) -> None:
    loop = FakeLoop(())
    loop.send_failure = OSError("UDP unavailable")
    monkeypatch.setattr(discovery_module, "current_loop", lambda: loop)

    with pytest.raises(SaneaDiscoveryError, match="UDP unavailable"):
        await SaneaDiscovery().discover()

    assert len(loop.requests) == 2
    assert udp_boundaries.channel.closed
