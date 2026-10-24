"""Steam UI helpers and independently launched Steam/Proton games."""

from pathlib import Path

from sanelib.protocol import ApplicationIdentity

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch


_CLIENT_EXECUTABLES = frozenset({"steam", "steamwebhelper"})
ENVIRONMENT_KEYS = frozenset({"SteamAppId", "SteamGameId", "XDG_SESSION_ID"})


def is_steam_client(process: ProcessIdentity) -> bool:
    """Recognize only Steam's own client executables, not arbitrary descendants."""
    return Path(process.exe).name in _CLIENT_EXECUTABLES


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

        titles = context.window_titles.get(process.pid, ())
        fallback_name = f"Steam game {game_id}"
        name = next((title.strip() for title in titles if title.strip() and len(title) <= 512
                     and not any(ord(character) < 32 for character in title)), fallback_name)
        return ApplicationMatch(
            root, ApplicationIdentity(ident=f"steam:game:{game_id}", name=name),
            group_same_executable=False, fallback_name=fallback_name,
        )

    if executable not in _CLIENT_EXECUTABLES:
        return None

    root = process

    for parent in context.walk_parents(process):

        if Path(parent.exe).name not in _CLIENT_EXECUTABLES:
            break

        root = parent

    return ApplicationMatch(root, ApplicationIdentity(ident="steam:client", name="Steam"), group_same_executable=False)
