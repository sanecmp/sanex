"""Tests for runtime-state reconciliation across configuration snapshots."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sanelib.protocol import Actor, ApplicationIdentity, Event, EventType, decode_config, iterate_event_records

from sanex.model.state import decode_runtime_state
from sanex.service.runtime import RuntimeRepository
from sanex.storage.events import EventPacket, EventStore
from sanex.storage.state import RuntimeStateStore


def repository(tmp_path: Path) -> RuntimeRepository:
    """Build a runtime repository contained in a temporary directory."""
    accounts_root = tmp_path / "accounts"
    return RuntimeRepository(
        RuntimeStateStore(accounts_root),
        EventStore(accounts_root),
        "4120b850-79a0-4b7c-b25c-42d6801a7b71",
    )


def test_config_change_preserves_stable_budgets_and_updates_active_limits(
    tmp_path: Path,
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["ident"] += 1
    rule = config_payload["accounts"][0]["limits"]["session_rules"][0]
    rule["max_duration"] = 900
    rule["break_duration"] = 300
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    store = RuntimeStateStore(tmp_path / "accounts")
    store.save(1001, state)
    runtimes = RuntimeRepository(
        store,
        EventStore(tmp_path / "accounts"),
        state.boot_ident,
    )

    migrated = runtimes.load(config.accounts[0], config.ident)

    assert migrated.config_ident == config.ident
    assert migrated.occurrences[0].sessions[0].spent == 812
    assert migrated.occurrences[0].sessions[0].max_duration == 900
    assert migrated.occurrences[0].sessions[0].break_duration == 300
    assert migrated.occurrences[0].apps[0].spent == 918
    assert migrated.occurrences[0].apps[0].active_wnds == []


def test_new_app_ident_starts_without_inheriting_removed_budget(
    tmp_path: Path,
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["ident"] += 1
    app = config_payload["accounts"][0]["limits"]["session_rules"][0][
        "app_rules"
    ][0]
    app["ident"] += 1
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    runtimes = repository(tmp_path)
    runtimes.state_store.save(1001, state)

    migrated = runtimes.load(config.accounts[0], config.ident)

    assert migrated.occurrences[0].sessions[0].spent == 812
    old_usage = migrated.occurrences[0].apps[0]
    assert old_usage.ident == 701
    assert old_usage.spent == 918
    assert old_usage.active_wnds == []
    assert all(usage.ident != 702 for usage in migrated.occurrences[0].apps)


def test_removing_limits_keeps_dormant_budget_history(
    tmp_path: Path,
    config_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> None:
    config_payload["ident"] += 1
    config_payload["accounts"][0].update({"apply": False, "limits": None})
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    runtimes = repository(tmp_path)
    runtimes.state_store.save(1001, state)

    migrated = runtimes.load(config.accounts[0], config.ident)

    assert migrated.occurrences[0].sessions[0].spent == 812
    assert migrated.occurrences[0].apps[0].spent == 918
    assert migrated.occurrences[0].apps[0].active_wnds == []


def test_new_boot_reports_saved_sessions_and_applications_before_clearing_them(
    tmp_path: Path, config_payload: dict[str, Any], runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    state.observed_at = 1_790_850_000
    runtimes = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    runtimes.state_store.save(1001, state)

    restored = runtimes.load(config.accounts[0], config.ident)
    endings = tuple(
        event
        for packet in runtimes.event_store.pending(1001)
        for event in iterate_event_records(packet.path.read_bytes())
    )

    assert [(event.type, event.run_ident, event.duration) for event in endings] == [
        (EventType.SESSION_END, 120, 3017), (EventType.PRC_END, 121, 3137),
    ]
    assert all(event.timestamp == state.observed_at and event.meta.actor is Actor.UNKNOWN for event in endings)
    assert restored.event_seq == 143
    assert restored.boot_ident == "synthetic-next-boot"
    assert restored.observed_at is None
    assert restored.sess_runs == restored.prc_runs == restored.wnds == []
    assert not restored.occurrences[0].sessions[0].active
    assert restored.occurrences[0].sessions[0].spent == 812
    assert runtimes.state_store.load(1001) == restored


def test_same_boot_preserves_runs_and_does_not_synthesize_endings(
    tmp_path: Path, config_payload: dict[str, Any], runtime_payload: dict[str, Any],
) -> None:
    state = decode_runtime_state(runtime_payload)
    config = decode_config(config_payload)
    runtimes = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), state.boot_ident)
    runtimes.state_store.save(1001, state)

    restored = runtimes.load(config.accounts[0], config.ident)

    assert restored == state
    assert runtimes.event_store.pending(1001) == ()
    assert runtimes.event_store.open_path(1001).read_bytes() == b""


def test_new_boot_delivers_pending_name_before_ending_the_launch_even_after_a_failed_state_save(
    tmp_path: Path, config_payload: dict[str, Any], runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    application = ApplicationIdentity(ident="steam:game:440", name="Team Fortress 2")
    state.prc_runs[0].application = application
    state.prc_runs[0].application_update_pending = True
    runtimes = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    runtimes.state_store.save(1001, state)

    with patch.object(RuntimeStateStore, "save", side_effect=OSError("synthetic save failure")):

        with pytest.raises(OSError, match="synthetic save failure"):
            runtimes.load(config.accounts[0], config.ident)

    restarted = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    restored = restarted.load(config.accounts[0], config.ident)
    events = tuple(
        event
        for packet in restarted.event_store.pending(1001)
        for event in iterate_event_records(packet.path.read_bytes())
    )

    assert [(event.type, event.run_ident) for event in events] == [
        (EventType.SESSION_END, 120), (EventType.PRC_UPDATE, 121), (EventType.PRC_END, 121),
    ]
    assert events[1].application == application
    assert events[2].duration == 3137
    assert restored.sess_runs == restored.prc_runs == []
    assert restored.event_seq == events[-1].seq


def test_boot_recovery_retries_failed_state_save_without_duplicate_endings(
    tmp_path: Path, config_payload: dict[str, Any], runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    state.prc_runs[0].reported = False
    state.prc_runs[0].run_ident = None
    runtimes = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    runtimes.state_store.save(1001, state)

    with patch.object(RuntimeStateStore, "save", side_effect=OSError("synthetic save failure")):

        with pytest.raises(OSError, match="synthetic save failure"):
            runtimes.load(config.accounts[0], config.ident)

    restarted = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    restored = restarted.load(config.accounts[0], config.ident)
    endings = tuple(
        event
        for packet in restarted.event_store.pending(1001)
        for event in iterate_event_records(packet.path.read_bytes())
    )

    assert len(endings) == 1
    assert endings[0].type is EventType.SESSION_END
    assert endings[0].timestamp == state.sess_runs[0].started + state.sess_runs[0].duration
    assert restored.sess_runs == restored.prc_runs == []
    assert restored.event_seq == endings[0].seq


def test_boot_recovery_retries_an_interrupted_append_without_losing_runs(
    tmp_path: Path, config_payload: dict[str, Any], runtime_payload: dict[str, Any],
) -> None:
    config = decode_config(config_payload)
    state = decode_runtime_state(runtime_payload)
    runtimes = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    runtimes.state_store.save(1001, state)
    append = EventStore.append

    def append_or_fail(store: EventStore, uid: int, event: Event) -> EventPacket | None:

        if event.type is EventType.PRC_END:
            raise OSError("synthetic append failure")

        return append(store, uid, event)

    with patch.object(EventStore, "append", append_or_fail):

        with pytest.raises(OSError, match="synthetic append failure"):
            runtimes.load(config.accounts[0], config.ident)

    restarted = RuntimeRepository(RuntimeStateStore(tmp_path), EventStore(tmp_path), "synthetic-next-boot")
    restored = restarted.load(config.accounts[0], config.ident)
    endings = tuple(
        event
        for packet in restarted.event_store.pending(1001)
        for event in iterate_event_records(packet.path.read_bytes())
    )

    assert [(event.type, event.run_ident) for event in endings] == [
        (EventType.SESSION_END, 120), (EventType.PRC_END, 121),
    ]
    assert restored.event_seq == 143
    assert restored.sess_runs == restored.prc_runs == []
