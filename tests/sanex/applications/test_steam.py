"""Steam UI attribution must not absorb games or independent launches."""

from collections.abc import Callable

import pytest

from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import WindowProcessResolver


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
