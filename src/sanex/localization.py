"""Translations selected from the desktop user's locale environment."""

from collections.abc import Callable, Iterable
from gettext import translation
from pathlib import Path


LOCALE_DIRECTORY = Path(__file__).with_name("locale")


def user_gettext(languages: Iterable[str] | None = None) -> Callable[[str], str]:
    """Return sanex translations for explicit or process-environment languages."""
    return translation(
        "sanex",
        localedir=LOCALE_DIRECTORY,
        languages=languages,
        fallback=True,
    ).gettext
