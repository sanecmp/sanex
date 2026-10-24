"""Steam UI attribution must not absorb games or independent launches."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from shutil import rmtree
from unittest.mock import Mock

import pytest

from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import ProcessReader, WindowProcessResolver
from sanex.service.steam_observation import SteamProcessObserver


def test_groups_only_known_client_helpers_and_keeps_games_separate(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    steam = process_create(exe="/apps/steam/steam", name="steam")
    helper = process_create(pid=110, parent_pid=100, exe="/apps/steam/steamwebhelper", name="steamwebhelper")
    process_create(pid=111, parent_pid=110, exe=helper.exe, name="steamwebhelper")
    process_create(pid=200, parent_pid=100, exe="/games/native", name="native", environment={"SteamAppId": "123"})
    process_create(pid=210, parent_pid=200, exe="/games/helper", environment={"SteamAppId": "123"})
    process_create(pid=300, parent_pid=100, exe="/games/native", environment={"SteamAppId": "123"})
    process_create(pid=400, parent_pid=100, exe="/games/other", environment={"SteamGameId": "456"})
    process_create(pid=500, parent_pid=100, exe="/apps/unrelated")
    windows = tuple(window_create(pid=pid) for pid in (100, 111, 210, 300, 400, 500))

    groups = resolver.resolve(windows, 1001)

    assert [group.process.pid for group in groups] == [100, 200, 300, 400, 500]
    assert [group.application.ident if group.application else None for group in groups] == [
        "steam:client", "steam:game:123", "steam:game:123", "steam:game:456", None,
    ]
    assert groups[0].application.name == "Steam"
    assert groups[0].process == steam
    assert [owner.pid for owner in groups[0].owners] == [100, 111]
    assert groups[1].ident != groups[2].ident


@pytest.mark.parametrize("environment", [
    {"SteamAppId": "0", "SteamGameId": "0"}, {"SteamAppId": "invalid"},
    {"SteamAppId": "-1"}, {"SteamAppId": "1" * 1000}, {"STEAM_COMPAT_APP_ID": "123"},
])
def test_does_not_claim_game_identity_from_sentinels_or_proton_installers(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, environment: dict[str, str],
) -> None:
    process_create(environment=environment)

    assert resolver.resolve((window_create(),), 1001)[0].application is None


def test_stops_at_uid_boundary_and_process_cycles(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(pid=99, uid=2000, exe="/usr/bin/steam")
    process_create(parent_pid=99, exe="/usr/bin/steamwebhelper")
    process_create(pid=200, parent_pid=201, exe="/usr/bin/steamwebhelper")
    process_create(pid=201, parent_pid=200, exe="/usr/bin/steamwebhelper")

    groups = resolver.resolve((window_create(), window_create(pid=200)), 1001)

    assert [group.process.pid for group in groups] == [100, 201]


def test_steam_discovery_is_idle_without_the_client_and_stops_scanning_after_its_exit(
    tmp_path: Path, process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver,
) -> None:
    process_create()
    scan = Mock(side_effect=ProcessReader(tmp_path).list_pids)
    observer = SteamProcessObserver(scan)
    resolver = replace(resolver, observer=observer)
    resolver.resolve((window_create(),), 1001, sess_ident="3")
    scan.assert_not_called()

    process_create(exe="/synthetic/steam", name="steam", started=101)
    game = process_create(pid=200, parent_pid=100, name="tf_linux64", environment={"SteamAppId": "440", "XDG_SESSION_ID": "3"})
    process_create(pid=201, parent_pid=200, environment={"SteamAppId": "440", "XDG_SESSION_ID": "3"})
    process_create(pid=300, uid=2000, environment={"SteamAppId": "999"})
    process_create(pid=400, environment={"SteamAppId": "888", "XDG_SESSION_ID": "4"})

    groups = resolver.resolve((window_create(),), 1001, sess_ident="3")

    assert [group.application.ident for group in groups] == ["steam:client", "steam:game:440"]
    assert groups[1].process == game
    assert groups[1].windows == ()
    assert scan.call_count == 1

    rmtree(tmp_path / "100")
    groups = resolver.resolve((), 1001, sess_ident="3")
    assert [group.application.ident for group in groups] == ["steam:game:440"]
    assert scan.call_count == 1

    rmtree(tmp_path / "200")
    rmtree(tmp_path / "201")
    assert resolver.resolve((), 1001, sess_ident="3") == ()
    resolver.retain_sessions(1001, frozenset())
    scan.assert_called_once()


def test_steam_window_and_process_sources_share_one_launch_and_use_the_window_title(
    tmp_path: Path, process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver,
) -> None:
    process_create(exe="/synthetic/steam", name="steam")
    game = process_create(pid=200, parent_pid=100, environment={"SteamAppId": "440"})
    window = window_create(pid=200).model_copy(update={"title": "Team Fortress 2"})
    scan = Mock(side_effect=ProcessReader(tmp_path).list_pids)
    resolver = replace(resolver, observer=SteamProcessObserver(scan))

    groups = resolver.resolve((window_create(), window), 1001, sess_ident="3")

    assert len(groups) == 2
    assert groups[1].process == game
    assert groups[1].windows == (window,)
    assert groups[1].application.name == "Team Fortress 2"
    assert groups[1].application.ident == "steam:game:440"


def test_reused_client_pid_does_not_keep_steam_process_scanning_enabled(
    tmp_path: Path, process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver,
) -> None:
    process_create(exe="/synthetic/steam", name="steam")
    scan = Mock(side_effect=ProcessReader(tmp_path).list_pids)
    resolver = replace(resolver, observer=SteamProcessObserver(scan))
    resolver.resolve((window_create(),), 1001, sess_ident="3")
    process_create(exe="/synthetic/unrelated", started=101)

    assert resolver.resolve((), 1001, sess_ident="3") == ()
    assert scan.call_count == 1
