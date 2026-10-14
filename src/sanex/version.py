"""Installed sanex distribution metadata."""

from importlib.metadata import PackageNotFoundError, version


def get_installed_version() -> str:
    """Return the installed sanex distribution version."""
    try:
        return version("sanecmp-sanex")

    except PackageNotFoundError:
        return "0+unknown"
