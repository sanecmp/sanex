"""Tests for safe same-account development composition."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sanelib.protocol import DiscoveredAccount

from sanex.exceptions import ServiceError
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.window import AtspiWindowSnapshot
from sanex.service.development import (
    CurrentAccountDiscovery,
    CurrentSessionLogind,
    CurrentSessionWindows,
    DevelopmentEnvironment,
    DevelopmentPaths,
)
from sanex.storage.config import ConfigStore


class FakeLogind:
    def __init__(self, sessions: tuple[LoginSession, ...]) -> None:
        self._sessions = sessions

    async def sessions(self) -> tuple[LoginSession, ...]:
        return self._sessions


class FakeAtspi:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True
        self.connected = False

    async def windows(self) -> AtspiWindowSnapshot:
        return AtspiWindowSnapshot()


def make_session(uid: int, ident: str, active: bool = True) -> LoginSession:
    """Build one graphical logind session for adapter tests."""
    return LoginSession(
        ident=ident,
        uid=uid,
        login="developer",
        path=f"/org/freedesktop/login1/session/_{ident}",
        started=1_791_040_000,
        type=SessionType.WAYLAND,
        state=SessionState.ACTIVE,
        active=active,
        idle=False,
        locked=False,
    )


@pytest.mark.asyncio
async def test_current_account_discovery_reports_only_invoking_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(
        pw_name="developer",
        pw_gecos="Development User,,",
    )
    monkeypatch.setattr("sanex.service.development.pwd.getpwuid", lambda uid: record)

    accounts = await CurrentAccountDiscovery(uid=1001).discover()

    assert accounts == (
        DiscoveredAccount(
            uid=1001,
            login="developer",
            name="Development User",
        ),
    )


@pytest.mark.asyncio
async def test_development_adapters_are_limited_to_current_session() -> None:
    own = make_session(1001, "own")
    another_session = make_session(1001, "other")
    another_uid = make_session(1002, "foreign")
    logind = CurrentSessionLogind(
        client=FakeLogind((own, another_session, another_uid)),
        uid=1001,
        session_ident="own",
    )
    backend = FakeAtspi()
    windows = CurrentSessionWindows(
        client=backend,
        uid=1001,
        session_ident="own",
    )

    assert await logind.sessions() == (own,)
    await windows.reconcile((own,))
    assert (await windows.windows(own)).windows == ()
    assert backend.connected

    with pytest.raises(ServiceError, match="another UID"):
        await windows.reconcile((another_uid,))

    with pytest.raises(ServiceError, match="disabled"):
        await windows.close_window(1001, "own", object())

    await windows.close()
    assert backend.closed


def test_environment_builds_unprivileged_non_enforcing_service(
    tmp_path: Path,
    config_payload: dict[str, Any],
) -> None:
    uid = os.getuid()
    config_payload["accounts"][0]["uid"] = uid
    paths = DevelopmentPaths(tmp_path)
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text(json.dumps(config_payload))
    environment = DevelopmentEnvironment(
        paths,
        uid=uid,
        environment=lambda: {"XDG_SESSION_ID": "own"},
    )

    registration = environment.create_registration()
    service = environment.create_service()

    assert registration.config_store.path == paths.config
    assert registration.pki.permanent_root == paths.pki
    assert registration.pki.pending_root == paths.pending_pki
    assert service.config == ConfigStore(paths.config).load()
    assert service.cycle_factory.enforcement_enabled is False
    assert service.sync_scheduler is None
    assert paths.accounts.joinpath(f"{uid}", "runtime.json").is_file()
