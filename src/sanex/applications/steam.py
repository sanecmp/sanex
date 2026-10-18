"""Steam UI helpers and independently launched Steam/Proton games."""

from pathlib import Path

from sanelib.protocol import ApplicationIdentity

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch


_CLIENT_EXECUTABLES = frozenset({"steam", "steamwebhelper"})
ENVIRONMENT_KEYS = frozenset({"SteamAppId", "SteamGameId"})


def read_game_id(process: ProcessIdentity, context: ApplicationContext) -> str | None:
    """Use Steam's game hints, not Proton's installer-compatible app ID alone."""
    environment = context.read_environment(process)

    for key in ("SteamAppId", "SteamGameId"):
        value = environment.get(key, "")

        if value.isascii() and value.isdecimal() and len(value) <= 20 and int(value) > 0:
            return f"{int(value)}"

    return None


def identify_steam(process: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch | None:
    """Merge only known UI helpers; a game ID never merges separate process trees."""
    executable = Path(process.exe).name
    game_id = read_game_id(process, context)

    if game_id is not None and executable not in _CLIENT_EXECUTABLES:
        root = process

        for parent in context.walk_parents(process):

            if Path(parent.exe).name in _CLIENT_EXECUTABLES or read_game_id(parent, context) != game_id:
                break

            root = parent

        return ApplicationMatch(
            root, ApplicationIdentity(ident=f"steam:game:{game_id}", name=f"Steam game {game_id}"), group_same_executable=False,
        )

    if executable not in _CLIENT_EXECUTABLES:
        return None

    root = process

    for parent in context.walk_parents(process):

        if Path(parent.exe).name not in _CLIENT_EXECUTABLES:
            break

        root = parent

    return ApplicationMatch(root, ApplicationIdentity(ident="steam:client", name="Steam"), group_same_executable=False)
