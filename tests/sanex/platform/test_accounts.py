"""Tests for local account discovery sources and fallback behavior."""

import logging
import pwd
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from dbus_fast import Variant

from sanex.exceptions import AccountDiscoveryError
from sanelib.protocol import DiscoveredAccount
from sanex.platform.accounts import AccountDiscovery, AccountsServiceSource, NssSource
from tests.sanex.platform.fakes import FakeBus, dbus_reply


def user_properties(sample: dict[str, Any]) -> dict[str, Variant]:
    """Build AccountsService variants from a datafixture entry."""
    return {
        "Uid": Variant("t", sample["uid"]),
        "UserName": Variant("s", sample["login"]),
        "RealName": Variant("s", sample["name"]),
        "SystemAccount": Variant("b", sample["system"]),
        "LocalAccount": Variant("b", sample["local"]),
        "Locked": Variant("b", sample["locked"]),
    }


@pytest.mark.asyncio
async def test_accounts_service_keeps_locked_and_nonlocal_human_accounts(
    account_discovery_payload: dict[str, Any],
) -> None:
    samples = account_discovery_payload["accounts_service"]
    paths = [sample["path"] for sample in samples]
    bus = FakeBus([dbus_reply([paths]), *(dbus_reply([user_properties(sample)]) for sample in samples)])
    source = AccountsServiceSource(bus_factory=lambda: bus)

    accounts = await source.discover()

    assert accounts == (
        DiscoveredAccount(uid=1001, login="child", name="Иван"),
        DiscoveredAccount(uid=2000, login="network-child", name="network-child"),
    )
    assert [message.member for message in bus.messages] == ["ListCachedUsers", "GetAll", "GetAll", "GetAll"]
    assert not bus.connected


@pytest.mark.asyncio
async def test_accounts_service_failure_is_wrapped_and_disconnects(
    account_discovery_payload: dict[str, Any],
) -> None:
    sample = account_discovery_payload["accounts_service"][0]
    properties = user_properties(sample)
    properties.pop("Locked")
    bus = FakeBus([dbus_reply([[sample["path"]]]), dbus_reply([properties])])

    with pytest.raises(AccountDiscoveryError, match="property Locked"):
        await AccountsServiceSource(bus_factory=lambda: bus).discover()

    assert not bus.connected


@pytest.mark.asyncio
async def test_nss_filters_uid_names_and_shells(
    tmp_path: Path,
    account_discovery_payload: dict[str, Any],
) -> None:
    login_defs = tmp_path / "login.defs"
    login_defs.write_text("UID_MIN 1000\n")
    shells = tmp_path / "shells"
    shells.write_text("/bin/bash\n/bin/dash\n/bin/false\n/usr/sbin/nologin\n")
    entries = tuple(pwd.struct_passwd(entry) for entry in account_discovery_payload["nss"])
    source = NssSource(login_defs, shells, passwd_provider=lambda: entries)

    accounts = await source.discover()

    assert accounts == (
        DiscoveredAccount(uid=1000, login="parent", name="parent"),
        DiscoveredAccount(uid=1001, login="child", name="Иван"),
    )


@pytest.mark.asyncio
async def test_discovery_uses_fallback_but_does_not_turn_two_failures_into_empty_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = DiscoveredAccount(uid=1001, login="child", name="Child")
    primary = SimpleNamespace(discover=AsyncMock(side_effect=AccountDiscoveryError("primary", OSError("down"))))
    fallback = SimpleNamespace(discover=AsyncMock(return_value=(account,)))

    assert await AccountDiscovery(primary=primary, fallback=fallback).discover() == (account,)

    fallback.discover.side_effect = AccountDiscoveryError("fallback", OSError("down"))

    with caplog.at_level(logging.ERROR):
        assert await AccountDiscovery(primary=primary, fallback=fallback).discover() is None

    assert "fallback account discovery failed" in caplog.text


@pytest.mark.asyncio
async def test_successful_empty_primary_snapshot_does_not_use_fallback() -> None:
    primary = SimpleNamespace(discover=AsyncMock(return_value=()))
    fallback = SimpleNamespace(discover=AsyncMock())

    assert await AccountDiscovery(primary=primary, fallback=fallback).discover() == ()
    fallback.discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_nss_does_not_wrap_unexpected_implementation_failure(
    tmp_path: Path,
) -> None:
    login_defs = tmp_path / "login.defs"
    login_defs.write_text("UID_MIN 1000\n")
    shells = tmp_path / "shells"
    shells.write_text("/bin/bash\n")

    def fail() -> list[pwd.struct_passwd]:
        raise RuntimeError("implementation bug")

    source = NssSource(login_defs, shells, passwd_provider=fail)

    with pytest.raises(RuntimeError, match="implementation bug"):
        await source.discover()
