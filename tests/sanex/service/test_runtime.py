"""Tests for runtime-state reconciliation across configuration snapshots."""

from pathlib import Path
from typing import Any

from sanelib.protocol import decode_config

from sanex.model.state import decode_runtime_state
from sanex.service.runtime import RuntimeRepository
from sanex.storage.events import EventStore
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
