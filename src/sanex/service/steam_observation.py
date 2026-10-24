"""Enable Steam game discovery only after observing its running client."""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..applications.common import ApplicationContext, ApplicationMatch
from ..applications.steam import identify_steam, is_steam_client
from ..exceptions import ProcessError
from ..model.process import ProcessIdentity


@dataclass(slots=True)
class SteamProcessObserver:
    """Find windowless games while Steam is alive; never read its installation files."""

    list_pids: Callable[[], tuple[int, ...]]
    _clients: dict[tuple[int, str], tuple[ProcessIdentity, ...]] = field(default_factory=dict, init=False)
    _games: dict[tuple[int, str], tuple[ApplicationMatch, ...]] = field(default_factory=dict, init=False)

    def retain_sessions(self, uid: int, sess_idents: frozenset[str]) -> None:
        """Release discovery state for ended or unmonitored login sessions."""

        for mapping in (self._clients, self._games):

            for key in tuple(mapping):

                if key[0] == uid and key[1] not in sess_idents:
                    del mapping[key]

    def observe(
        self, uid: int, sess_ident: str, context: ApplicationContext, matches: tuple[ApplicationMatch, ...],
    ) -> tuple[ApplicationMatch, ...]:
        """Share identity and metadata reads; stop full scans when Steam exits."""
        key = (uid, sess_ident)
        clients = {process.pid: process for process in self._clients.get(key, ())}

        for match in matches:

            if is_steam_client(match.process):
                clients[match.process.pid] = match.process

            elif match.application is not None and match.application.ident.startswith("steam:game:"):

                for parent in context.walk_parents(match.process):

                    if is_steam_client(parent):
                        clients[parent.pid] = parent

        live_clients = []

        for previous in clients.values():

            try:
                current = context.read_process(previous.pid)

            except ProcessError:
                # A temporary permission failure is not proof of process exit.
                live_clients.append(previous)
                continue

            if current is not None and current.uid == uid and current.started == previous.started and is_steam_client(current):
                live_clients.append(current)

        self._clients[key] = tuple(live_clients)
        known_games = {match.process.pid: match for match in self._games.get(key, ())}
        candidates = self.list_pids() if live_clients else tuple(known_games)
        games = {}

        for pid in candidates:

            try:
                process = context.read_process(pid)

            except ProcessError:

                if pid in known_games:
                    games[pid] = known_games[pid]

                continue

            if process is None or process.uid != uid:
                continue

            if not live_clients and process.started != known_games[pid].process.started:
                continue

            environment = context.read_environment(process)
            session = environment.get("XDG_SESSION_ID")

            if session and session != sess_ident:
                continue

            match = identify_steam(process, context)

            if match is not None and match.application.ident.startswith("steam:game:"):
                root = match.process
                games[root.pid] = match

        self._games[key] = tuple(games.values())
        return self._games[key]
