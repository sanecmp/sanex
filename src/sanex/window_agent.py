"""Internal per-session AT-SPI agent entry point."""

import argparse
import asyncio
from collections.abc import Sequence

from .service.window_agent import run_agent


def create_parser() -> argparse.ArgumentParser:
    """Create the internal window-agent argument parser."""
    parser = argparse.ArgumentParser(prog="sanex-window-agent")
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--session", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one window agent on an inherited private socket."""
    arguments = create_parser().parse_args(argv)
    asyncio.run(run_agent(arguments.fd, arguments.uid, arguments.session))
    return 0
