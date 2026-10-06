"""Local user account discovery through AccountsService with NSS fallback."""

import asyncio
import logging
import pwd
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from dbus_fast import Message

from ..exceptions import AccountDiscoveryError
from sanelib.protocol import DiscoveredAccount
from .dbus import (
    DBUS_OPERATION_ERRORS,
    SystemBus,
    property_value,
    raise_dbus_error,
    system_bus,
)


_ACCOUNTS_DESTINATION = "org.freedesktop.Accounts"
_ACCOUNTS_PATH = "/org/freedesktop/Accounts"
_ACCOUNTS_INTERFACE = "org.freedesktop.Accounts"
_USER_INTERFACE = "org.freedesktop.Accounts.User"
_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
_DEFAULT_UID_MIN = 1_000
_EXCLUDED_LOGINS = frozenset(
    {
        "games",
        "gnome-initial-setup",
        "halt",
        "lightdm",
        "mysql",
        "news",
        "nfsnobody",
        "pcap",
        "postgres",
        "root",
        "shutdown",
        "sync",
        "uucp",
    }
)
_EXCLUDED_SHELLS = frozenset({"false", "nologin"})
logger = logging.getLogger(__name__)


class _AccountSource(Protocol):
    async def discover(self) -> tuple[DiscoveredAccount, ...]: ...


@dataclass(frozen=True, slots=True)
class AccountsServiceSource:
    """Read the complete cached-user snapshot from the system D-Bus."""

    bus_factory: Callable[[], SystemBus] = system_bus

    async def discover(self) -> tuple[DiscoveredAccount, ...]:
        bus = self.bus_factory()
        connected = False
        try:
            await bus.connect()
            connected = True
            paths = await self._list_cached_users(bus)
            accounts = [await self._read_user(bus, path) for path in paths]
            return _normalize(account for account in accounts if account is not None)

        except DBUS_OPERATION_ERRORS as error:
            raise AccountDiscoveryError("AccountsService", error) from error

        finally:

            if connected:
                bus.disconnect()

    @staticmethod
    async def _list_cached_users(bus: SystemBus) -> Sequence[str]:
        reply = await bus.call(
            Message(
                destination=_ACCOUNTS_DESTINATION,
                path=_ACCOUNTS_PATH,
                interface=_ACCOUNTS_INTERFACE,
                member="ListCachedUsers",
            )
        )
        raise_dbus_error(reply)

        if len(reply.body) != 1 or not isinstance(reply.body[0], list):
            raise ValueError("ListCachedUsers returned an invalid body")

        paths = reply.body[0]

        if not all(isinstance(path, str) and path for path in paths):
            raise ValueError("ListCachedUsers returned an invalid object path")

        return paths

    @staticmethod
    async def _read_user(bus: SystemBus, path: str) -> DiscoveredAccount | None:
        reply = await bus.call(
            Message(
                destination=_ACCOUNTS_DESTINATION,
                path=path,
                interface=_PROPERTIES_INTERFACE,
                member="GetAll",
                signature="s",
                body=[_USER_INTERFACE],
            )
        )
        raise_dbus_error(reply)

        if len(reply.body) != 1 or not isinstance(reply.body[0], dict):
            raise ValueError(f"GetAll returned an invalid body for {path}")

        properties = reply.body[0]
        uid = property_value(properties, "Uid", int)
        login = property_value(properties, "UserName", str)
        real_name = property_value(properties, "RealName", str)
        system_account = property_value(properties, "SystemAccount", bool)
        property_value(properties, "LocalAccount", bool)
        property_value(properties, "Locked", bool)

        if system_account:
            return None

        return DiscoveredAccount(uid=uid, login=login, name=real_name or login)


@dataclass(frozen=True, slots=True)
class NssSource:
    """Enumerate human accounts through NSS when AccountsService is unavailable."""

    login_defs_path: Path = Path("/etc/login.defs")
    shells_path: Path = Path("/etc/shells")
    passwd_provider: Callable[[], Sequence[pwd.struct_passwd]] = pwd.getpwall

    async def discover(self) -> tuple[DiscoveredAccount, ...]:
        try:
            return await asyncio.to_thread(self._discover_sync)

        except (OSError, UnicodeError, ValueError) as error:
            raise AccountDiscoveryError("NSS", error) from error

    def _discover_sync(self) -> tuple[DiscoveredAccount, ...]:
        uid_min = self._read_uid_min()
        valid_shells = self._read_shells()
        accounts: list[DiscoveredAccount] = []

        for entry in self.passwd_provider():
            shell = entry.pw_shell

            if entry.pw_uid < uid_min or entry.pw_name in _EXCLUDED_LOGINS:
                continue

            if not shell or Path(shell).name in _EXCLUDED_SHELLS or shell not in valid_shells:
                continue

            real_name = entry.pw_gecos.split(",", maxsplit=1)[0].strip()
            accounts.append(
                DiscoveredAccount(
                    uid=entry.pw_uid,
                    login=entry.pw_name,
                    name=real_name or entry.pw_name,
                )
            )

        return _normalize(accounts)

    def _read_uid_min(self) -> int:
        try:
            lines = self.login_defs_path.read_text().splitlines()

        except (OSError, UnicodeError):
            return _DEFAULT_UID_MIN

        for line in lines:
            fields = line.partition("#")[0].split()

            if len(fields) < 2 or fields[0] != "UID_MIN":
                continue

            try:
                value = int(fields[1])

            except ValueError:
                return _DEFAULT_UID_MIN

            return value if value >= 0 else _DEFAULT_UID_MIN

        return _DEFAULT_UID_MIN

    def _read_shells(self) -> frozenset[str]:
        lines = self.shells_path.read_text().splitlines()
        return frozenset(
            line
            for raw_line in lines
            if (line := raw_line.partition("#")[0].strip())
        )


@dataclass(frozen=True, slots=True)
class AccountDiscovery:
    """Use AccountsService first and NSS only as a complete fallback source."""

    primary: _AccountSource = field(default_factory=AccountsServiceSource)
    fallback: _AccountSource = field(default_factory=NssSource)

    async def discover(self) -> tuple[DiscoveredAccount, ...] | None:
        try:
            return await self.primary.discover()

        except AccountDiscoveryError as error:
            logger.warning("%s", error)

        try:
            return await self.fallback.discover()

        except AccountDiscoveryError as error:
            logger.error("%s", error)
            return None


def _normalize(accounts: Iterable[DiscoveredAccount]) -> tuple[DiscoveredAccount, ...]:
    ordered = tuple(sorted(accounts, key=lambda account: account.uid))

    for previous, current in zip(ordered, ordered[1:], strict=False):

        if previous.uid == current.uid:
            raise ValueError(f"duplicate account UID {current.uid}")

    return ordered
