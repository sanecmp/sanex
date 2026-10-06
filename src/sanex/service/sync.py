"""Bridge between the synchronization worker and service-owned state."""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from sanelib.protocol import Config, DiscoveredAccount, SyncRequest, SyncResponse

from ..platform.accounts import AccountDiscovery
from ..storage.events import EventPacket, EventStore
from ..sync.commands import CommandStore
from ..sync.exchange import SyncSnapshot
from ..sync.log import TechnicalLog
from ..version import installed_version


class SyncAccountDiscovery(Protocol):
    async def discover(self) -> tuple[DiscoveredAccount, ...] | None: ...


class SyncCommandExecutor(Protocol):
    async def run(self) -> None: ...


class SyncUpdateRecovery(Protocol):
    def reconcile(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceSyncState:
    """Serialize sync mutations with accounting through the service lock."""

    config: Callable[[], Config]
    apply_config: Callable[[str | bytes | bytearray], Awaitable[bool]]
    state_lock: asyncio.Lock
    event_store: EventStore
    command_store: CommandStore
    command_executor: SyncCommandExecutor
    update_recovery: SyncUpdateRecovery
    accounts: SyncAccountDiscovery = field(default_factory=AccountDiscovery)
    technical_log: TechnicalLog = field(default_factory=TechnicalLog)
    hostname: Callable[[], str] = socket.gethostname
    package_version: Callable[[], str] = installed_version

    async def prepare(self) -> SyncSnapshot:
        """Discover accounts and atomically freeze request data and event packets."""
        accounts, log_tail = await asyncio.gather(
            self.accounts.discover(),
            asyncio.to_thread(self.technical_log.tail),
        )

        async with self.state_lock:
            config = self.config()
            packets: list[EventPacket] = []
            event_store = self.event_store
            uids = {account.uid for account in config.accounts}
            uids.update(event_store.find_stored_uids())

            for uid in sorted(uids):
                event_store.seal(uid)
                packets.extend(event_store.pending(uid))

            request = SyncRequest(
                hostname=self.hostname(),
                version=self.package_version(),
                config_ident=config.ident,
                accounts=accounts,
                command_results=self.command_store.results,
            )
            return SyncSnapshot(request, log_tail, tuple(packets))

    async def accept_control(
        self,
        snapshot: SyncSnapshot,
        response: SyncResponse,
    ) -> None:
        """Apply validated config and durably reconcile commands from a 200 response."""

        if response.config is not None:
            await self.apply_config(response.config.model_dump_json())

        async with self.state_lock:
            self.command_store.accept(
                snapshot.request.command_results,
                response.commands,
            )
            self.update_recovery.reconcile()

    async def acknowledge(self, packet: EventPacket) -> None:
        """Remove one remotely accepted immutable packet under the state lock."""

        async with self.state_lock:
            self.event_store.acknowledge(packet)

    async def finish(self) -> None:
        """Execute commands after every synchronization channel has completed."""
        await self.command_executor.run()
