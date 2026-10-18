"""Flatpak application identity, including the AT-SPI D-Bus proxy."""

from pathlib import Path
from collections.abc import Callable

from sanelib.protocol import ApplicationIdentity

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch, parse_desktop_id, parse_key_file


ENVIRONMENT_KEYS = frozenset({"FLATPAK_ID"})


def identify_flatpak(
    process: ProcessIdentity, context: ApplicationContext, *, find_name: Callable[[str], str],
) -> ApplicationMatch | None:
    """Attribute a Flatpak proxy without pretending its PID is the sandbox application's PID."""
    is_proxy = Path(process.exe).name == "xdg-dbus-proxy"
    if not is_proxy and not context.read_environment(process).get("FLATPAK_ID"):
        return None

    data = context.read_file(process, "root/.flatpak-info")
    value = parse_key_file(data, "Application", "name")
    app_id = parse_desktop_id(value) if value else None

    if app_id is None:
        return None

    return ApplicationMatch(process, ApplicationIdentity(ident=f"flatpak:{app_id}", name=find_name(app_id)))
