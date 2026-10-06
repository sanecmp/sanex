"""Composition and lifecycle of the root sanex system service."""

import asyncio
import logging
import os
import signal
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sanelib.protocol import Config

from ..accounting.clock import ActiveTimeClock
from ..accounting.cycle import AccountCycle
from ..accounting.runtime import AccountRuntime
from ..accounting.schedule import ScheduleResolver
from ..exceptions import SaneaException, ServiceError, StorageError
from ..indicator.server import (
    DEFAULT_INDICATOR_SOCKET_PATH,
    IndicatorStatusServer,
)
from ..indicator.status import IndicatorStatusBuilder
from ..model.config import parse_config
from ..model.indicator import IndicatorStatus
from ..model.state import RuntimeState
from ..platform.accounts import AccountDiscovery
from ..platform.logind import LogindClient
from ..platform.process import ProcessReader, WindowProcessResolver
from ..storage.config import DEFAULT_CONFIG_PATH, ConfigStore
from ..storage.events import EventStore
from ..storage.pki import PkiPaths
from ..storage.state import RuntimeStateStore
from ..sync.client import SyncHttpClient
from ..sync.commands import (
    DEFAULT_COMMAND_STATE_PATH,
    CommandExecutor,
    CommandStore,
)
from ..sync.exchange import SyncExchange
from ..sync.log import TechnicalLog
from ..sync.scheduler import SyncScheduler
from ..sync.update import (
    DEFAULT_INSTALL_CONFIG_PATH,
    DEFAULT_UPDATE_LOG_PATH,
    DEFAULT_UPDATE_STATE_PATH,
    ExecProcessReplacer,
    InstallConfigStore,
    UpdateHandler,
    UpdateRecovery,
    UpdateStateStore,
)
from .runtime import RuntimeRepository
from ..version import installed_version
from .sync import ServiceSyncState
from .supervisor import Supervisor
from .window_agents import WindowAgentManager


DEFAULT_BOOT_IDENT_PATH = Path("/proc/sys/kernel/random/boot_id")
DEFAULT_WINDOW_AGENT_PATH = Path("/opt/sanex/bin/sanex-window-agent")
logger = logging.getLogger(__name__)


class ServiceWalkResult(Protocol):
    """Timestamp required from an accounting walk result."""

    timestamp: int


class ServiceSupervisor(Protocol):
    """Accounting supervisor operations used by the service loop."""

    async def walk(
        self,
        existing: bool = False,
        elapsed: int | None = None,
    ) -> ServiceWalkResult: ...


class ServiceLogind(Protocol):
    """Logind lifecycle and sleep notifications used by the service."""

    async def connect(self) -> None: ...

    async def start_sleep_monitoring(self) -> None: ...

    async def stop_sleep_monitoring(self) -> None: ...

    async def next_sleep_change(self) -> bool: ...

    def close(self) -> None: ...


class ServiceWindowAgents(Protocol):
    """Window-agent cleanup used during service shutdown."""

    async def close(self) -> None: ...


class ServiceStateSaver(Protocol):
    """Runtime-state persistence used during service shutdown."""

    def save(self, uid: int, state: RuntimeState) -> RuntimeState: ...


class ServiceSyncScheduler(Protocol):
    """Background synchronization scheduler owned by the service."""

    async def run(self, stop_requested: asyncio.Event) -> None: ...


class ServiceAsyncCloser(Protocol):
    """Asynchronous network resource closed during service shutdown."""

    async def close(self) -> None: ...


class ServiceIndicatorServer(Protocol):
    """Local read-only status channel owned by the service."""

    async def start(self) -> None: ...

    def publish(self, timestamp: int) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AccountCycleFactory:
    """Build consistently wired account cycles for one config snapshot."""

    process_resolver: WindowProcessResolver
    terminator: LogindClient
    window_closer: WindowAgentManager
    event_store: EventStore
    state_store: RuntimeStateStore
    enforcement_enabled: bool = True

    def build(
        self,
        config: Config,
        runtimes: Mapping[int, AccountRuntime],
    ) -> dict[int, AccountCycle]:
        return {
            account.uid: AccountCycle(
                runtime=runtimes[account.uid],
                config=config,
                process_resolver=self.process_resolver,
                terminator=self.terminator,
                window_closer=self.window_closer,
                event_appender=self.event_store,
                state_saver=self.state_store,
                enforcement_enabled=self.enforcement_enabled,
            )
            for account in config.accounts
        }


@dataclass(slots=True)
class SanexService:
    """Run periodic accounting and own all system-facing service resources."""

    config: Config
    supervisor: ServiceSupervisor
    logind: ServiceLogind
    window_agents: ServiceWindowAgents
    state_saver: ServiceStateSaver
    runtimes: Mapping[int, AccountRuntime]
    clock: ActiveTimeClock
    config_store: ConfigStore
    runtime_repository: RuntimeRepository
    cycle_factory: AccountCycleFactory
    install_signal_handlers: bool = True
    sync_scheduler: ServiceSyncScheduler | None = None
    sync_closer: ServiceAsyncCloser | None = None
    indicator_server: ServiceIndicatorServer | None = None
    _stop_requested: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
    )
    _state_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _connected: bool = field(default=False, init=False, repr=False)
    _monitoring: bool = field(default=False, init=False, repr=False)
    _updating: bool = field(default=False, init=False, repr=False)
    _indicator_started: bool = field(default=False, init=False, repr=False)

    def request_stop(self) -> None:
        """Request an orderly stop at the next event-loop boundary."""
        self._stop_requested.set()

    async def apply_config(self, data: str | bytes | bytearray) -> bool:
        """Durably apply a new config and enforce it on current sessions."""
        config = parse_config(data)

        async with self._state_lock:
            current_config = self.config

            if config.ident == current_config.ident:

                if config != current_config:
                    raise ServiceError(
                        f"config ident {config.ident} was reused for different content"
                    )

                return False

            await self.supervisor.walk()
            self._save_runtimes()
            config = self.config_store.save(data)
            previous = self.runtimes
            runtimes: dict[int, AccountRuntime] = {}

            for account in config.accounts:
                runtime = previous.get(account.uid)

                if runtime is None:
                    state = self.runtime_repository.load(account, config.ident)
                    runtime = AccountRuntime(account.uid, state)

                else:
                    self.runtime_repository.apply(
                        account.uid,
                        runtime.state,
                        account,
                        config.ident,
                    )

                runtimes[account.uid] = runtime

            self.config = config
            self.runtimes = runtimes
            self.supervisor = Supervisor(
                session_source=self.logind,
                window_source=self.window_agents,
                cycles=self.cycle_factory.build(config, runtimes),
                clock=self.clock,
            )
            result = await self.supervisor.walk(existing=True)
            indicator_server = self.indicator_server

            if indicator_server is not None:
                indicator_server.publish(result.timestamp)

            self._log_limit_changes(current_config, config)
            logger.info(
                "Applied config %d, replacing %d, with %d account(s)",
                config.ident, current_config.ident, len(config.accounts),
            )

        return True

    async def run(self) -> None:
        """Connect resources, run accounting walks and shut down durably."""
        loop = asyncio.get_running_loop()
        installed_signals = self._install_signals(loop)
        sync_task: asyncio.Task[None] | None = None
        try:
            await self.logind.connect()
            self._connected = True
            await self.logind.start_sleep_monitoring()
            self._monitoring = True
            indicator_server = self.indicator_server

            if indicator_server is not None:
                await indicator_server.start()
                self._indicator_started = True

            await self._walk(existing=True)

            if self.sync_scheduler is not None:
                sync_task = asyncio.create_task(
                    self.sync_scheduler.run(self._stop_requested),
                    name="sanex-sync",
                )

            logger.info(
                "Sanex service started with config %d and %d account(s)",
                self.config.ident, len(self.config.accounts),
            )

            while not self._stop_requested.is_set():
                stop_task = asyncio.create_task(self._stop_requested.wait())
                sleep_task = asyncio.create_task(self.logind.next_sleep_change())
                try:
                    background_tasks = {stop_task, sleep_task}

                    if sync_task is not None:
                        background_tasks.add(sync_task)

                    done, pending_tasks = await asyncio.wait(
                        background_tasks,
                        timeout=self.config.walk_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if sync_task is not None and sync_task in done:
                        error = sync_task.exception()

                        if error is not None:
                            raise error

                        if not self._stop_requested.is_set():
                            raise ServiceError(
                                "synchronization scheduler stopped unexpectedly"
                            )

                    if stop_task in done:
                        break

                    if sleep_task in done:
                        await self._handle_sleep_change(sleep_task.result())

                    else:
                        await self._walk()

                finally:

                    for task in (stop_task, sleep_task):

                        if not task.done():
                            task.cancel()

                    await asyncio.gather(
                        stop_task,
                        sleep_task,
                        return_exceptions=True,
                    )

        finally:
            self._remove_signals(loop, installed_signals)

            if sync_task is not None:

                if self._updating:
                    await asyncio.gather(sync_task, return_exceptions=True)

                else:
                    sync_task.cancel()
                    await asyncio.gather(sync_task, return_exceptions=True)

            if not self._updating:
                await self._shutdown()
                logger.info("Sanex service stopped")

    async def prepare_update(self) -> None:
        """Stop service work and close resources before replacing the process."""
        logger.info("Preparing sanex update")
        self._updating = True
        self._stop_requested.set()

        async with self._state_lock:
            await self._shutdown()

    def authorize_indicator(self, uid: int, sess_ident: str) -> bool:
        """Allow a peer to read only its own currently observed session."""
        runtime = self.runtimes.get(uid)
        return runtime is not None and runtime.session_run(sess_ident) is not None

    def get_indicator_status(
        self,
        uid: int,
        sess_ident: str,
        timestamp: int,
    ) -> IndicatorStatus | None:
        """Build current indicator status for an authenticated local peer."""
        runtime = self.runtimes.get(uid)
        config = self.config
        account = next(
            (entry for entry in config.accounts if entry.uid == uid),
            None,
        )

        if (
            runtime is None
            or account is None
            or runtime.session_run(sess_ident) is None
        ):
            return None

        return IndicatorStatusBuilder(
            runtime,
            ScheduleResolver.for_config(config),
        ).build(account, sess_ident, timestamp)

    async def _shutdown(self) -> None:
        errors: list[tuple[str, Exception]] = []
        try:
            self._save_runtimes()

        except Exception as error:
            errors.append(("save runtime state", error))

        if self.sync_closer is not None:
            try:
                await self.sync_closer.close()

            except Exception as error:
                errors.append(("close synchronization client", error))

        indicator_server = self.indicator_server

        if self._indicator_started and indicator_server is not None:
            try:
                await indicator_server.close()
                self._indicator_started = False

            except Exception as error:
                errors.append(("close indicator server", error))

        try:
            await self.window_agents.close()

        except Exception as error:
            errors.append(("close window agents", error))

        if self._monitoring:
            try:
                await self.logind.stop_sleep_monitoring()
                self._monitoring = False

            except Exception as error:
                errors.append(("stop sleep monitoring", error))

        if self._connected:
            try:
                self.logind.close()
                self._connected = False

            except Exception as error:
                errors.append(("close logind client", error))

        for operation, error in errors:
            logger.error(
                "Unable to %s: %s", operation, error,
                exc_info=(type(error), error, error.__traceback__),
            )

        if errors:
            error = errors[0][1]

            if isinstance(error, SaneaException):
                raise error

            raise ServiceError("service shutdown failed") from error

    async def _handle_sleep_change(self, sleeping: bool) -> None:

        if sleeping:
            elapsed = self.clock.set_sleeping(True)
            await self._walk(elapsed=elapsed)
            return

        self.clock.set_sleeping(False)
        await self._walk()

    async def _walk(
        self,
        existing: bool = False,
        elapsed: int | None = None,
    ) -> None:

        async with self._state_lock:
            result = await self.supervisor.walk(existing=existing, elapsed=elapsed)
            indicator_server = self.indicator_server

            if indicator_server is not None:
                indicator_server.publish(result.timestamp)

    def _save_runtimes(self) -> None:

        for uid, runtime in sorted(self.runtimes.items()):
            self.state_saver.save(uid, runtime.state)

    @staticmethod
    def _log_limit_changes(previous: Config, current: Config) -> None:
        previous_apply = {account.uid: account.apply for account in previous.accounts}
        current_apply = {account.uid: account.apply for account in current.accounts}

        for uid in sorted(previous_apply.keys() | current_apply.keys()):
            before = previous_apply.get(uid, False)
            after = current_apply.get(uid, False)

            if before == after:
                continue

            action = "Enabled" if after else "Disabled"
            logger.info("%s limits for UID %d", action, uid)

    def _install_signals(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[signal.Signals, ...]:

        if not self.install_signal_handlers:
            return ()

        installed = []

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_stop)

            except (NotImplementedError, RuntimeError):
                logger.warning("Unable to install handler for %s", signum.name)

            else:
                installed.append(signum)

        return tuple(installed)

    @staticmethod
    def _remove_signals(
        loop: asyncio.AbstractEventLoop,
        installed: tuple[signal.Signals, ...],
    ) -> None:

        for signum in installed:
            loop.remove_signal_handler(signum)


@dataclass(slots=True)
class SanexServiceFactory:
    """Load durable state and compose one complete service instance."""

    config_store: ConfigStore = field(
        default_factory=lambda: ConfigStore(DEFAULT_CONFIG_PATH)
    )
    state_store: RuntimeStateStore = field(default_factory=RuntimeStateStore)
    event_store: EventStore = field(default_factory=EventStore)
    boot_ident_path: Path = DEFAULT_BOOT_IDENT_PATH
    window_agent_path: Path = DEFAULT_WINDOW_AGENT_PATH
    logind_factory: Callable[[], LogindClient] = LogindClient
    clock_factory: Callable[[], ActiveTimeClock] = ActiveTimeClock
    pki_paths: PkiPaths = field(default_factory=PkiPaths)
    command_state_path: Path = DEFAULT_COMMAND_STATE_PATH
    install_config_path: Path = DEFAULT_INSTALL_CONFIG_PATH
    update_state_path: Path = DEFAULT_UPDATE_STATE_PATH
    update_log_path: Path = DEFAULT_UPDATE_LOG_PATH
    indicator_socket_path: Path = DEFAULT_INDICATOR_SOCKET_PATH
    indicator_socket_mode: int = 0o666
    account_discovery: AccountDiscovery = field(default_factory=AccountDiscovery)
    technical_log: TechnicalLog = field(default_factory=TechnicalLog)
    sync_client_factory: Callable[[ssl.SSLContext], SyncHttpClient] = SyncHttpClient
    package_version: Callable[[], str] = installed_version
    effective_uid: Callable[[], int] = os.geteuid
    window_agents_factory: Callable[[Path], ServiceWindowAgents] = WindowAgentManager
    require_root: bool = True
    enforcement_enabled: bool = True
    update_enabled: bool = True
    sync_delay_limit: float | None = None

    def create(self) -> SanexService:
        """Load configuration and account state, then wire all adapters."""

        if self.require_root and self.effective_uid() != 0:
            raise ServiceError("sanex service must run as root")

        config = self.config_store.load()

        if config is None:
            raise ServiceError(f"configuration does not exist: {self.config_store.path}")

        boot_ident = self._read_boot_ident()
        logind = self.logind_factory()
        clock = self.clock_factory()
        window_agents = self.window_agents_factory(self.window_agent_path)
        runtime_repository = RuntimeRepository(
            self.state_store,
            self.event_store,
            boot_ident,
        )
        cycle_factory = AccountCycleFactory(
            process_resolver=WindowProcessResolver(ProcessReader()),
            terminator=logind,
            window_closer=window_agents,
            event_store=self.event_store,
            state_store=self.state_store,
            enforcement_enabled=self.enforcement_enabled,
        )
        runtimes = {
            account.uid: AccountRuntime(
                account.uid,
                runtime_repository.load(account, config.ident),
            )
            for account in config.accounts
        }
        supervisor = Supervisor(
            session_source=logind,
            window_source=window_agents,
            cycles=cycle_factory.build(config, runtimes),
            clock=clock,
        )
        service = SanexService(
            config=config,
            supervisor=supervisor,
            logind=logind,
            window_agents=window_agents,
            state_saver=self.state_store,
            runtimes=runtimes,
            clock=clock,
            config_store=self.config_store,
            runtime_repository=runtime_repository,
            cycle_factory=cycle_factory,
        )
        service.indicator_server = IndicatorStatusServer(
            path=self.indicator_socket_path,
            mode=self.indicator_socket_mode,
            authorize=service.authorize_indicator,
            provide_status=service.get_indicator_status,
        )
        command_store = CommandStore(self.command_state_path)
        update_state_store = UpdateStateStore(self.update_state_path)
        update_recovery = UpdateRecovery(
            command_store,
            update_state_store,
            self.package_version,
        )
        update_recovery.reconcile()
        self._configure_sync(
            service,
            command_store,
            update_state_store,
            update_recovery,
        )
        return service

    def _configure_sync(
        self,
        service: SanexService,
        command_store: CommandStore,
        update_state_store: UpdateStateStore,
        update_recovery: UpdateRecovery,
    ) -> None:
        material = (
            self.pki_paths.ca,
            self.pki_paths.certificate,
            self.pki_paths.key,
        )
        present = tuple(path.is_file() for path in material)

        if not any(present):
            logger.warning("Sanex is not registered; synchronization is disabled")
            return

        context = self.pki_paths.client_context()
        client = self.sync_client_factory(context)
        handlers = {}

        if self.update_enabled:
            handlers["update"] = UpdateHandler(
                before_exec=service.prepare_update,
                installed_version=self.package_version,
                install_store=InstallConfigStore(self.install_config_path),
                state_store=update_state_store,
                process_replacer=ExecProcessReplacer(self.update_log_path),
            )

        command_executor = CommandExecutor(
            command_store,
            handlers,
        )
        state = ServiceSyncState(
            config=lambda: service.config,
            apply_config=service.apply_config,
            state_lock=service._state_lock,
            event_store=self.event_store,
            command_store=command_store,
            command_executor=command_executor,
            update_recovery=update_recovery,
            accounts=self.account_discovery,
            technical_log=self.technical_log,
        )
        exchange = SyncExchange(state=state, client=client)
        service.sync_scheduler = SyncScheduler(
            config=lambda: service.config,
            exchange=exchange,
            delay_limit=self.sync_delay_limit,
        )
        service.sync_closer = client

    def _read_boot_ident(self) -> str:
        try:
            boot_ident = self.boot_ident_path.read_text().strip()

        except OSError as error:
            raise StorageError("read", self.boot_ident_path, error) from error

        if not boot_ident:
            raise ServiceError(f"boot ident is empty: {self.boot_ident_path}")

        return boot_ident
