"""Tests for system-service composition and lifecycle."""

import asyncio
import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sanelib.protocol import Config, decode_config

from sanex.accounting.clock import ActiveTimeClock
from sanex.accounting.runtime import AccountRuntime
from sanex.exceptions import ServiceError
from sanex.model.state import RuntimeState, decode_runtime_state
from sanex.model.window import AtspiWindowSnapshot
from sanex.platform.process import ProcessReader, WindowProcessResolver
from sanex.service.daemon import (
    AccountCycleFactory,
    SanexService,
    SanexServiceFactory,
)
from sanex.service.runtime import RuntimeRepository
from sanex.storage.config import ConfigStore
from sanex.storage.events import EventStore
from sanex.storage.pki import PkiPaths
from sanex.storage.state import RuntimeStateStore


@dataclass(frozen=True, slots=True)
class FakeWalkResult:
    timestamp: int


class FakeSupervisor:
    def __init__(self, clock: ActiveTimeClock | None = None) -> None:
        self.clock = clock
        self.calls: list[tuple[bool, int | None]] = []
        self.on_walk: Callable[[], None] | None = None

    async def walk(
        self,
        existing: bool = False,
        elapsed: int | None = None,
    ) -> FakeWalkResult:

        if elapsed is None and self.clock is not None:
            self.clock.advance()

        self.calls.append((existing, elapsed))

        if self.on_walk is not None:
            self.on_walk()

        return FakeWalkResult(timestamp=1_790_956_800 + len(self.calls) - 1)


class FakeLogind:
    def __init__(self) -> None:
        self.sleep_changes: list[bool] = []
        self.connected = False
        self.monitoring = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def start_sleep_monitoring(self) -> None:
        self.monitoring = True

    async def stop_sleep_monitoring(self) -> None:
        self.monitoring = False

    async def next_sleep_change(self) -> bool:

        while not self.sleep_changes:
            await asyncio.sleep(0)

        return self.sleep_changes.pop(0)

    async def sessions(self) -> tuple[object, ...]:
        return ()

    async def terminate(self, ident: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeWindowAgents:
    def __init__(self) -> None:
        self.closed = False

    async def reconcile(self, sessions: tuple[object, ...]) -> None:
        return None

    async def windows(self, session: object) -> AtspiWindowSnapshot:
        return AtspiWindowSnapshot()

    async def close_window(
        self,
        uid: int,
        sess_ident: str,
        window: object,
    ) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class FakeStateSaver:
    def __init__(self) -> None:
        self.saved: list[tuple[int, RuntimeState]] = []

    def save(self, uid: int, state: RuntimeState) -> RuntimeState:
        snapshot = state.model_copy(deep=True)
        self.saved.append((uid, snapshot))
        return snapshot


class FakeIndicatorServer:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.published: list[int] = []

    async def start(self) -> None:
        self.started = True

    def publish(self, timestamp: int) -> None:
        self.published.append(timestamp)

    async def close(self) -> None:
        self.closed = True


def write_config(path: Path, payload: dict[str, Any]) -> None:
    """Write a datafixture configuration to a temporary service path."""
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))


def service_factory(
    tmp_path: Path,
    config_payload: dict[str, Any],
    boot_ident: str,
) -> SanexServiceFactory:
    """Build a factory whose durable resources are contained in tmp_path."""
    config_path = tmp_path / "config" / "config.json"
    accounts_root = tmp_path / "state" / "accounts"
    boot_path = tmp_path / "boot_id"
    write_config(config_path, config_payload)
    boot_path.write_text(f"{boot_ident}\n")
    return SanexServiceFactory(
        config_store=ConfigStore(config_path),
        state_store=RuntimeStateStore(accounts_root),
        event_store=EventStore(accounts_root),
        boot_ident_path=boot_path,
        window_agent_path=Path("/opt/sanex/bin/sanex-window-agent"),
        pki_paths=PkiPaths(tmp_path / "pki"),
        command_state_path=tmp_path / "state" / "commands.json",
        install_config_path=tmp_path / "config" / "install.json",
        update_state_path=tmp_path / "state" / "update.json",
        update_log_path=tmp_path / "log" / "sanex.log",
        effective_uid=lambda: 0,
    )


def runtime_for_service(config: Config) -> AccountRuntime:
    """Build an empty runtime owned by a lifecycle-only service test."""
    return AccountRuntime(
        config.accounts[0].uid,
        RuntimeState(
            config_ident=config.ident,
            boot_ident="boot-current",
            wnd_seq=0,
            event_seq=0,
            break_till=None,
            sess_runs=[],
            prc_runs=[],
            wnds=[],
            occurrences=[],
        ),
    )


def lifecycle_service(
    tmp_path: Path,
    config: Config,
    supervisor: FakeSupervisor,
    logind: FakeLogind,
    agents: FakeWindowAgents,
    saver: FakeStateSaver,
    runtime: AccountRuntime,
    clock: ActiveTimeClock,
) -> SanexService:
    """Compose a service with observable lifecycle adapters."""
    config_store = ConfigStore(tmp_path / "config.json")
    config_store.save(config.model_dump_json())
    state_store = RuntimeStateStore(tmp_path / "accounts")
    event_store = EventStore(tmp_path / "accounts")
    repository = RuntimeRepository(state_store, event_store, "boot-current")
    cycle_factory = AccountCycleFactory(
        process_resolver=WindowProcessResolver(ProcessReader(tmp_path / "proc")),
        terminator=logind,
        window_closer=agents,
        event_store=event_store,
        state_store=state_store,
    )
    return SanexService(
        config=config,
        supervisor=supervisor,
        logind=logind,
        window_agents=agents,
        state_saver=saver,
        runtimes={runtime.uid: runtime},
        clock=clock,
        config_store=config_store,
        runtime_repository=repository,
        cycle_factory=cycle_factory,
        install_signal_handlers=False,
    )


def test_factory_requires_root(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    factory = service_factory(tmp_path, config_payload, "boot-current")
    factory.effective_uid = lambda: 1000

    with pytest.raises(ServiceError, match="must run as root"):
        factory.create()


def test_factory_creates_and_recovers_account_state(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    factory = service_factory(tmp_path, config_payload, "boot-current")

    service = factory.create()

    runtime = service.runtimes[1001]
    assert runtime.state.config_ident == service.config.ident
    assert runtime.state.boot_ident == "boot-current"
    assert factory.state_store.load(1001) == runtime.state
    assert factory.event_store.open_path(1001).is_file()


def test_factory_can_build_unprivileged_non_enforcing_service(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    factory = service_factory(tmp_path, config_payload, "boot-current")
    factory.effective_uid = lambda: 1001
    factory.require_root = False
    factory.enforcement_enabled = False

    service = factory.create()

    assert all(
        cycle.enforcement_enabled is False
        for cycle in service.cycle_factory.build(
            service.config,
            service.runtimes,
        ).values()
    )


def test_factory_resets_only_active_state_after_reboot(
    tmp_path: Path,
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    factory = service_factory(tmp_path, config_payload, "boot-new")
    previous = decode_runtime_state(runtime_payload)
    previous_break = previous.break_till
    previous_spent = previous.occurrences[0].apps[0].spent
    factory.state_store.save(1001, previous)

    service = factory.create()

    state = service.runtimes[1001].state
    assert state.boot_ident == "boot-new"
    assert state.sess_runs == []
    assert state.prc_runs == []
    assert state.wnds == []
    assert state.break_till == previous_break
    assert state.occurrences[0].apps[0].spent == previous_spent
    assert state.occurrences[0].apps[0].active_wnds == []
    assert not state.occurrences[0].sessions[0].active


def test_factory_migrates_state_from_another_configuration(
    tmp_path: Path,
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["ident"] += 1
    factory = service_factory(tmp_path, config_payload, "boot-current")
    factory.state_store.save(1001, decode_runtime_state(runtime_payload))

    service = factory.create()

    state = service.runtimes[1001].state
    assert state.config_ident == config_payload["ident"]
    assert state.occurrences[0].sessions[0].spent == 812
    assert state.occurrences[0].apps[0].spent == 918


@pytest.mark.asyncio
async def test_service_starts_walks_saves_and_closes_resources(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    clock = ActiveTimeClock(now=lambda: 0)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    indicator = FakeIndicatorServer()
    service.indicator_server = indicator
    supervisor.on_walk = service.request_stop

    await service.run()

    assert supervisor.calls == [(True, None)]
    assert saver.saved == [(runtime.uid, runtime.state)]
    assert agents.closed
    assert logind.closed
    assert not logind.monitoring
    assert indicator.started
    assert indicator.published == [1_790_956_800]
    assert indicator.closed


@pytest.mark.asyncio
async def test_service_accounts_active_time_before_sleep(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    readings = iter((0, 1_000_000_000, 3_500_000_000))
    clock = ActiveTimeClock(now=readings.__next__)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    logind.sleep_changes.append(True)
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    supervisor.on_walk = lambda: (
        service.request_stop()
        if len(supervisor.calls) == 2
        else None
    )

    await service.run()

    assert supervisor.calls == [(True, None), (False, 2)]
    assert clock.sleeping


@pytest.mark.asyncio
async def test_apply_config_rebuilds_cycles_and_persists_new_snapshot(
    tmp_path: Path,
    config_payload: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = decode_config(config_payload)
    clock = ActiveTimeClock(now=lambda: 0)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    config_payload["ident"] += 1
    config_payload["accounts"][0]["limits"]["session_rules"][0][
        "max_duration"
    ] = 900
    config_payload["accounts"][0]["apply"] = False

    with caplog.at_level("INFO"):
        changed = await service.apply_config(json.dumps(config_payload))

    assert changed
    assert supervisor.calls == [(False, None)]
    assert service.config.ident == config_payload["ident"]
    assert service.config_store.load() == service.config
    assert service.runtimes[1001] is runtime
    assert runtime.state.config_ident == service.config.ident
    assert "Disabled limits for UID 1001" in caplog.text
    assert f"Applied config {config_payload["ident"]}" in caplog.text


@pytest.mark.asyncio
async def test_apply_config_rejects_reused_ident_before_accounting(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    clock = ActiveTimeClock(now=lambda: 0)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    config_payload["walk_interval"] += 1

    with pytest.raises(ServiceError, match="reused for different content"):
        await service.apply_config(json.dumps(config_payload))

    assert supervisor.calls == []


class FakeSyncScheduler:
    def __init__(self, on_run: Callable[[], None]) -> None:
        self.on_run = on_run
        self.started = False
        self.finished = False

    async def run(self, stop_requested: asyncio.Event) -> None:
        self.started = True
        self.on_run()
        try:
            await stop_requested.wait()
        finally:
            self.finished = True


class FakeAsyncCloser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FailedSyncScheduler:
    async def run(self, stop_requested: asyncio.Event) -> None:
        raise RuntimeError("synchronization bug")


@pytest.mark.asyncio
async def test_service_owns_sync_scheduler_and_network_client(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    clock = ActiveTimeClock(now=lambda: 0)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    scheduler = FakeSyncScheduler(service.request_stop)
    closer = FakeAsyncCloser()
    service.sync_scheduler = scheduler
    service.sync_closer = closer

    await service.run()

    assert scheduler.started
    assert scheduler.finished
    assert closer.closed


@pytest.mark.asyncio
async def test_service_propagates_background_sync_failure_and_closes_resources(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    clock = ActiveTimeClock(now=lambda: 0)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    closer = FakeAsyncCloser()
    service.sync_scheduler = FailedSyncScheduler()
    service.sync_closer = closer

    with pytest.raises(RuntimeError, match="synchronization bug"):
        await service.run()

    assert saver.saved == [(runtime.uid, runtime.state)]
    assert closer.closed
    assert agents.closed
    assert logind.closed


class FakeUpdateScheduler:
    def __init__(self, service: SanexService) -> None:
        self.service = service
        self.finished = False

    async def run(self, stop_requested: asyncio.Event) -> None:
        await self.service.prepare_update()
        self.finished = True


@pytest.mark.asyncio
async def test_service_prepares_resources_for_process_replacement(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    clock = ActiveTimeClock(now=lambda: 0)
    supervisor = FakeSupervisor(clock)
    logind = FakeLogind()
    agents = FakeWindowAgents()
    saver = FakeStateSaver()
    runtime = runtime_for_service(config)
    service = lifecycle_service(
        tmp_path,
        config,
        supervisor,
        logind,
        agents,
        saver,
        runtime,
        clock,
    )
    scheduler = FakeUpdateScheduler(service)
    closer = FakeAsyncCloser()
    service.sync_scheduler = scheduler
    service.sync_closer = closer

    await service.run()

    assert scheduler.finished
    assert saver.saved == [(runtime.uid, runtime.state)]
    assert closer.closed
    assert agents.closed
    assert logind.closed
    assert not logind.monitoring


class FakePkiPaths:
    def __init__(self, root: Path) -> None:
        self.ca = root / "ca.crt"
        self.certificate = root / "client.crt"
        self.key = root / "client.key"
        root.mkdir()

        for path in (self.ca, self.certificate, self.key):
            path.touch()

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context()


def test_factory_wires_sync_when_registration_material_exists(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    factory = service_factory(tmp_path, config_payload, "boot-current")
    factory.pki_paths = FakePkiPaths(tmp_path / "registered-pki")
    client = FakeAsyncCloser()
    factory.sync_client_factory = lambda context: client  # type: ignore[assignment]

    service = factory.create()

    assert service.sync_scheduler is not None
    assert service.sync_closer is client


def test_factory_disables_update_handler_for_safe_development(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    factory = service_factory(tmp_path, config_payload, "boot-current")
    factory.pki_paths = FakePkiPaths(tmp_path / "registered-pki")
    factory.sync_client_factory = lambda context: FakeAsyncCloser()  # type: ignore[assignment]
    factory.update_enabled = False

    service = factory.create()

    executor = service.sync_scheduler.exchange.state.command_executor
    assert executor.handlers == {}
