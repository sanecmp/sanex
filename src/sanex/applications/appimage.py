"""Identify AppImages independently of their temporary mount directories."""

from pathlib import PurePosixPath

from sanelib.protocol import ApplicationIdentity

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch


ENVIRONMENT_KEYS = frozenset({"APPIMAGE", "APPDIR"})


def identify_appimage(process: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch | None:
    """Use the original image path only for executables inside its declared AppDir."""
    environment = context.read_environment(process)
    locations = (environment.get("APPIMAGE", ""), environment.get("APPDIR", ""))

    if any(not value or len(value) > 4000 or any(ord(character) < 32 for character in value) for value in locations):
        return None

    image, directory = (PurePosixPath(value) for value in locations)
    executable = PurePosixPath(process.exe)

    if any(not path.is_absolute() or ".." in path.parts for path in (image, directory, executable)):
        return None

    if not image.name or directory == PurePosixPath("/") or executable == directory:
        return None

    # APPIMAGE and APPDIR are inherited by subprocesses outside the image too.
    if not executable.is_relative_to(directory) or image.is_relative_to(directory):
        return None

    return ApplicationMatch(process, ApplicationIdentity(ident=f"appimage:{image}", name=image.stem[:512]))
