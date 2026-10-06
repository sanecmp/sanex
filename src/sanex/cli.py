"""Command-line interface for sanex."""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from .exceptions import SaneaException, StorageError
from .service.daemon import SanexServiceFactory
from .service.development import DevelopmentEnvironment, DevelopmentPaths
from .sync.registration import RegistrationServiceFactory
from .utils.logging import DEFAULT_LOG_PATH, logging_context
from .version import installed_version


logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    """Create the sanex command-line parser."""
    parser = argparse.ArgumentParser(prog="sanex")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {installed_version()}",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("service", help="run the sanex system service")
    register = commands.add_parser(
        "register",
        help="register this computer with sanea",
    )
    register.add_argument("code", help="one-use registration code")
    develop = commands.add_parser(
        "develop",
        help="run sanex safely under the current account",
    )
    develop.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local" / "state" / "sanex-development",
        help="isolated development state directory",
    )
    develop.set_defaults(development_parser=develop)
    development_commands = develop.add_subparsers(dest="development_command")
    development_commands.add_parser(
        "service",
        help="run foreground accounting and synchronization",
    )
    development_register = development_commands.add_parser(
        "register",
        help="register the isolated development client",
    )
    development_register.add_argument("code", help="one-use registration code")
    return parser


def run_service() -> None:
    """Compose and run the root system service until it is stopped."""
    service = SanexServiceFactory().create()
    asyncio.run(service.run())


def run_registration(code: str) -> None:
    """Run one explicit registration or resume a pending registration."""
    service = RegistrationServiceFactory().create()
    asyncio.run(service.run(code))


def create_development_environment(state_dir: Path) -> DevelopmentEnvironment:
    """Create one isolated development composition from a user-facing path."""
    return DevelopmentEnvironment(DevelopmentPaths(state_dir.expanduser().resolve()))


def run_development_service(state_dir: Path) -> None:
    """Run safe foreground sanex accounting under the current account."""
    environment = create_development_environment(state_dir)
    service = environment.create_service()
    asyncio.run(service.run())


def run_development_registration(code: str, state_dir: Path) -> None:
    """Register one isolated development sanex client."""
    environment = create_development_environment(state_dir)
    service = environment.create_registration()
    asyncio.run(service.run(code))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sanex command-line interface."""
    parser = create_parser()
    arguments = parser.parse_args(argv)

    if arguments.command is None:
        parser.print_help()
        return 0

    if arguments.command == "develop" and arguments.development_command is None:
        arguments.development_parser.print_help()
        return 0

    log_path = DEFAULT_LOG_PATH

    if arguments.command == "develop":
        log_path = create_development_environment(arguments.state_dir).paths.log

    try:

        with logging_context(log_path):

            try:

                if arguments.command == "service":
                    run_service()

                elif arguments.command == "register":
                    run_registration(arguments.code)

                elif arguments.development_command == "service":
                    run_development_service(arguments.state_dir)

                else:
                    run_development_registration(arguments.code, arguments.state_dir)

            except SaneaException as error:
                logger.error("Command failed: %s", error)
                return 1

            except Exception:
                logger.exception("Unexpected command failure")
                raise

    except StorageError:
        # Preparation was diagnosed while the stderr handler was still open.
        return 1

    return 0
