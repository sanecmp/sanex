"""Service composition: explicitly select application recognition providers."""

from functools import partial
from pathlib import Path

from ..applications import ApplicationResolver
from ..applications.common import ApplicationProvider, MetadataReader
from ..applications.desktop import ENVIRONMENT_KEYS as DESKTOP_ENVIRONMENT_KEYS, DesktopCatalog, identify_desktop
from ..applications.flatpak import ENVIRONMENT_KEYS as FLATPAK_ENVIRONMENT_KEYS, identify_flatpak
from ..applications.gjs import identify_gjs
from ..applications.steam import ENVIRONMENT_KEYS as STEAM_ENVIRONMENT_KEYS, identify_steam


def create_application_resolver(
    proc_root: Path = Path("/proc"), *, catalog: DesktopCatalog | None = None,
) -> ApplicationResolver:
    """Wire the supported providers without coupling the generic resolver to them."""
    catalog = DesktopCatalog() if catalog is None else catalog
    return ApplicationResolver(
        metadata=MetadataReader(proc_root),
        providers=(
            ApplicationProvider(identify_steam, STEAM_ENVIRONMENT_KEYS),
            ApplicationProvider(partial(identify_flatpak, find_name=catalog.find_name), FLATPAK_ENVIRONMENT_KEYS),
            ApplicationProvider(partial(identify_desktop, catalog=catalog), DESKTOP_ENVIRONMENT_KEYS),
            ApplicationProvider(identify_gjs),
        ),
    )
