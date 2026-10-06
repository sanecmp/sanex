"""Executable composition for the per-session desktop indicator."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..exceptions import IndicatorError, SaneaException
from ..localization import user_gettext
from ..model.indicator import IndicatorStatus
from ..platform.logind import LogindClient
from ..utils.logging import logging_context
from ..utils.resources import close_async_resources, close_resources
from ..version import installed_version
from .about import AboutDialog
from .client import IndicatorStatusClient
from .dbus import IndicatorDesktopService
from .presentation import IndicatorPresentation, IndicatorPresentationBuilder
from .server import DEFAULT_INDICATOR_SOCKET_PATH


logger = logging.getLogger(__name__)


def replace_indicator_process() -> None:
    """Replace the current indicator through its stable entry-point path."""
    executable = Path(sys.argv[0])

    if not executable.is_absolute():
        executable = Path.cwd() / executable

    executable_path = f"{executable.absolute()}"
    try:
        os.execv(
            executable_path,
            [executable_path, *sys.argv[1:]],
        )

    except OSError as error:
        raise IndicatorError("restart after update", error) from error


class StatusClient(Protocol):
    async def run(
        self,
        stop_requested: asyncio.Event,
        receive: Callable[[IndicatorStatus | None], None],
    ) -> None: ...


class DesktopService(Protocol):
    async def start(self) -> bool: ...

    def update(self, presentation: IndicatorPresentation) -> None: ...

    def close(self) -> None: ...


class AboutService(Protocol):
    async def close(self) -> None: ...


@dataclass(slots=True)
class IndicatorApplication:
    """Coordinate socket status, presentation and desktop D-Bus exports."""

    client: StatusClient
    presentation_builder: IndicatorPresentationBuilder
    desktop: DesktopService
    about: AboutService
    running_version: str | None = None
    read_installed_version: Callable[[], str] = installed_version
    replace_process: Callable[[], None] = replace_indicator_process
    install_signal_handlers: bool = True
    _stop_requested: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
    )
    _service_available: bool = field(default=False, init=False, repr=False)
    _restart_requested: bool = field(default=False, init=False, repr=False)

    def request_stop(self) -> None:
        """Request orderly indicator shutdown."""
        self._stop_requested.set()

    async def run(self) -> None:
        """Run the desktop item until a signal or caller requests shutdown."""
        loop = asyncio.get_running_loop()
        installed_signals = self._install_signals(loop)
        desktop = self.desktop
        try:

            if not await desktop.start():
                return

            await self.client.run(
                self._stop_requested,
                self._update_status,
            )
        finally:
            await close_async_resources(
                ("remove indicator signals", lambda: self._remove_signals(loop, installed_signals)),
                ("close About dialog", self.about.close),
                ("close desktop service", desktop.close),
            )

        if self._restart_requested:
            self.replace_process()

    def _update_status(self, status: IndicatorStatus | None) -> None:
        service_available = self._service_available
        now_available = status is not None

        if now_available != service_available:
            self._service_available = now_available

            if self._request_restart():
                return

        presentation = self.presentation_builder.build(status)
        self.desktop.update(presentation)

    def _request_restart(self) -> bool:
        running_version = self.running_version

        if running_version is None:
            return False

        installed = self.read_installed_version()

        if installed == running_version:
            return False

        self._restart_requested = True
        self._stop_requested.set()
        return True

    def _install_signals(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[signal.Signals, ...]:

        if not self.install_signal_handlers:
            return ()

        installed = []

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_stop)

            except (NotImplementedError, RuntimeError):
                continue

            installed.append(signum)

        return tuple(installed)

    @staticmethod
    def _remove_signals(
        loop: asyncio.AbstractEventLoop,
        installed: tuple[signal.Signals, ...],
    ) -> None:

        for signum in installed:
            loop.remove_signal_handler(signum)


async def create_application(
    socket_path: Path,
    sess_ident: str | None = None,
) -> IndicatorApplication:
    """Discover the current session and compose one indicator application."""

    if sess_ident is None:
        logind = LogindClient()
        try:
            await logind.connect()
            sess_ident = await logind.get_session_ident(os.getpid())
        finally:
            close_resources(("close session discovery", logind.close))

    version = installed_version()
    builder = IndicatorPresentationBuilder(version, translate=user_gettext())
    presentation = builder.build(None)
    about = AboutDialog(presentation.about)
    desktop = IndicatorDesktopService(
        presentation=presentation,
        open_about=about.open,
    )
    return IndicatorApplication(
        client=IndicatorStatusClient(sess_ident, socket_path),
        presentation_builder=builder,
        desktop=desktop,
        about=about,
        running_version=version,
    )


def create_parser() -> argparse.ArgumentParser:
    """Create the standalone indicator argument parser."""
    parser = argparse.ArgumentParser(prog="sanex-indicator")
    parser.add_argument(
        "--socket",
        type=Path,
        default=DEFAULT_INDICATOR_SOCKET_PATH,
        help="sanex status socket path",
    )
    parser.add_argument(
        "--session",
        help="logind session identity; normally discovered automatically",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {installed_version()}",
    )
    return parser


async def run_indicator(socket_path: Path, sess_ident: str | None = None) -> None:
    """Compose and run one desktop indicator."""
    application = await create_application(socket_path, sess_ident)
    await application.run()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone desktop indicator entry point."""
    arguments = create_parser().parse_args(argv)

    with logging_context(path=None):

        try:
            asyncio.run(run_indicator(arguments.socket, arguments.session))

        except SaneaException as error:
            logger.error("Indicator failed: %s", error)
            return 1

        except KeyboardInterrupt:
            return 0

    return 0
