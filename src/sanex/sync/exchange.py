"""One complete discovery-led exchange with sanea."""

import logging
from dataclasses import dataclass
from typing import Protocol

from sanelib.protocol import SyncRequest, SyncResponse

from ..storage.events import EventPacket
from .client import EventUploadResult, LogUploadResult
from .discovery import SaneaDiscovery, SaneaEndpoint


logger = logging.getLogger(__name__)
_SUCCESSFUL_EVENT_RESULTS = frozenset(
    {
        EventUploadResult.SAVED,
        EventUploadResult.ALREADY_SAVED,
    }
)


@dataclass(frozen=True, slots=True)
class SyncSnapshot:
    """Immutable local input fixed before endpoint discovery."""

    request: SyncRequest
    log_tail: bytes
    packets: tuple[EventPacket, ...]


class ExchangeDiscovery(Protocol):
    async def discover(self) -> SaneaEndpoint | None: ...


class ExchangeClient(Protocol):
    async def sync(self, base_url: str, request: SyncRequest) -> SyncResponse: ...

    async def upload_log(self, base_url: str, content: bytes) -> LogUploadResult: ...

    async def upload_events(
        self,
        base_url: str,
        packet: EventPacket,
    ) -> EventUploadResult: ...


class ExchangeState(Protocol):
    """State-owner operations called at synchronization boundaries."""

    async def prepare(self) -> SyncSnapshot: ...

    async def accept_control(
        self,
        snapshot: SyncSnapshot,
        response: SyncResponse,
    ) -> None: ...

    async def acknowledge(self, packet: EventPacket) -> None: ...

    async def finish(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SyncExchange:
    """Run control, log and event channels in their required order."""

    state: ExchangeState
    client: ExchangeClient
    discovery: ExchangeDiscovery = SaneaDiscovery()

    async def run(self) -> bool:
        """Return whether one complete attempt reached a terminal result."""
        snapshot = await self.state.prepare()
        endpoint = await self.discovery.discover()

        if endpoint is None:
            return False

        response = await self.client.sync(endpoint.base_url, snapshot.request)
        await self.state.accept_control(snapshot, response)

        log_result = await self.client.upload_log(
            endpoint.base_url,
            snapshot.log_tail,
        )

        if log_result is not LogUploadResult.SAVED:
            logger.error("Sanea rejected the technical log tail: %s", log_result)

        for packet in sorted(
            snapshot.packets,
            key=lambda item: (item.uid, item.first_seq),
        ):
            result = await self.client.upload_events(endpoint.base_url, packet)

            if result in _SUCCESSFUL_EVENT_RESULTS:
                await self.state.acknowledge(packet)
                continue

            logger.error(
                "Sanea rejected event packet %s for UID %s: %s",
                packet.sha256,
                packet.uid,
                result,
            )

        await self.state.finish()
        return True
