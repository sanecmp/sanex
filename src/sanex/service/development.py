"""Safe foreground composition for same-account development."""

import os
import pwd
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sanelib.protocol import DiscoveredAccount

from ..exceptions import ServiceError
from ..model.session import LoginSession
from ..model.window import AtspiWindow, AtspiWindowSnapshot
from ..platform.atspi import AtspiClient
from ..platform.logind import LogindClient
from ..storage.config import ConfigStore
from ..storage.events import EventStore
from ..storage.pki import PkiPaths, RegistrationPkiStore
from ..storage.state import RuntimeStateStore
from ..sync.log import TechnicalLog
from ..sync.registration import RegistrationService
from .daemon import SanexService, SanexServiceFactory


@dataclass(frozen=True, slots=True)
class DevelopmentPaths:
    """All sanex development state derived from one isolated root."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config" / "config.json"

    @property
    def pki(self) -> Path:
        return self.root / "config" / "pki"

    @property
    def pending_pki(self) -> Path:
        return self.root / "config" / "pending-pki"

    @property
    def accounts(self) -> Path:
        return self.root / "state" / "accounts"

    @property
    def commands(self) -> Path:
        return self.root / "state" / "commands.json"

    @property
    def install(self) -> Path:
        return self.root / "config" / "install.json"

    @property
    def update(self) -> Path:
        return self.root / "state" / "update.json"

    @property
    def log(self) -> Path:
        return self.root / "sanex.log"

    @property
    def indicator_socket(self) -> Path:
        return self.root / "run" / "indicator.sock"


@dataclass(frozen=True, slots=True)
class CurrentAccountDiscovery:
    """Report only the account that started the development process."""

    uid: int = field(default_factory=os.getuid)

    async def discover(self) -> tuple[DiscoveredAccount, ...]:
        record = pwd.getpwuid(self.uid)
        login = record.pw_name
        display_name = record.pw_gecos.partition(",")[0].strip() or login
        return (
            DiscoveredAccount(
                uid=self.uid,
                login=login,
                name=display_name,
            ),
        )


@dataclass(slots=True)
class CurrentSessionLogind:
    """Expose only the invoking user's current graphical login session."""

    client: LogindClient = field(default_factory=LogindClient)
    uid: int = field(default_factory=os.getuid)
    session_ident: str | None = None

    async def connect(self) -> None:
        await self.client.connect()

    def close(self) -> None:
        self.client.close()

    async def start_sleep_monitoring(self) -> None:
        await self.client.start_sleep_monitoring()

    async def stop_sleep_monitoring(self) -> None:
        await self.client.stop_sleep_monitoring()

    async def next_sleep_change(self) -> bool:
        return await self.client.next_sleep_change()

    async def sessions(self) -> tuple[LoginSession, ...]:
        sessions = await self.client.sessions()
        current = tuple(session for session in sessions if self._is_current(session))

        if len(current) > 1:
            raise ServiceError("development mode found multiple current sessions")

        return current

    async def terminate(self, ident: str) -> None:
        raise ServiceError("session termination is disabled in development mode")

    def _is_current(self, session: LoginSession) -> bool:

        if session.uid != self.uid:
            return False

        session_ident = self.session_ident

        if session_ident is not None:
            return session.ident == session_ident

        return session.active


@dataclass(slots=True)
class CurrentSessionWindows:
    """Observe the invoking session directly without a privileged child agent."""

    client: AtspiClient = field(default_factory=AtspiClient)
    uid: int = field(default_factory=os.getuid)
    session_ident: str | None = None
    _connected: bool = field(default=False, init=False, repr=False)
    _sessions: frozenset[str] = field(default_factory=frozenset, init=False, repr=False)

    async def reconcile(self, sessions: tuple[LoginSession, ...]) -> None:

        for session in sessions:
            self._validate_session(session)

        self._sessions = frozenset(session.ident for session in sessions)

        if sessions and not self._connected:
            await self.client.connect()
            self._connected = True

        elif not sessions and self._connected:
            self.client.close()
            self._connected = False

    async def windows(self, session: LoginSession) -> AtspiWindowSnapshot:
        self._validate_session(session)

        if session.ident not in self._sessions or not self._connected:
            raise ServiceError("development window source is not reconciled")

        return await self.client.windows()

    async def close_window(
        self,
        uid: int,
        sess_ident: str,
        window: AtspiWindow,
    ) -> bool:
        raise ServiceError("window closing is disabled in development mode")

    async def close(self) -> None:
        self._sessions = frozenset()

        if self._connected:
            self.client.close()
            self._connected = False

    def _validate_session(self, session: LoginSession) -> None:

        if session.uid != self.uid:
            raise ServiceError("development mode cannot observe another UID")

        session_ident = self.session_ident

        if session_ident is not None and session.ident != session_ident:
            raise ServiceError("development mode cannot observe another session")


@dataclass(frozen=True, slots=True)
class DevelopmentEnvironment:
    """Compose unprivileged sanex processes inside one development state root."""

    paths: DevelopmentPaths
    uid: int = field(default_factory=os.getuid)
    environment: Callable[[], Mapping[str, str]] = lambda: os.environ

    def create_registration(self) -> RegistrationService:
        paths = self.paths
        return RegistrationService(
            pki=RegistrationPkiStore(
                permanent_root=paths.pki,
                pending_root=paths.pending_pki,
                required_uid=self.uid,
            ),
            config_store=ConfigStore(paths.config),
            accounts=CurrentAccountDiscovery(self.uid),
        )

    def create_service(self) -> SanexService:
        paths = self.paths
        uid = self.uid
        session_ident = self.environment().get("XDG_SESSION_ID") or None
        logind = CurrentSessionLogind(uid=uid, session_ident=session_ident)
        windows = CurrentSessionWindows(uid=uid, session_ident=session_ident)
        factory = SanexServiceFactory(
            config_store=ConfigStore(paths.config),
            state_store=RuntimeStateStore(paths.accounts),
            event_store=EventStore(paths.accounts),
            pki_paths=PkiPaths(paths.pki),
            command_state_path=paths.commands,
            install_config_path=paths.install,
            update_state_path=paths.update,
            update_log_path=paths.log,
            indicator_socket_path=paths.indicator_socket,
            indicator_socket_mode=0o600,
            account_discovery=CurrentAccountDiscovery(uid),
            technical_log=TechnicalLog(paths.log),
            logind_factory=lambda: logind,
            window_agents_factory=lambda executable: windows,
            require_root=False,
            enforcement_enabled=False,
            update_enabled=False,
            sync_delay_limit=2,
        )
        return factory.create()
