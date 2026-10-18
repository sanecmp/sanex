"""Distinguish applications sharing the GNOME JavaScript interpreter."""

from pathlib import Path, PurePosixPath

from sanelib.protocol import ApplicationIdentity

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch


def identify_gjs(process: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch | None:
    """Identify the script, never a global interpreter alias."""

    if Path(process.exe).name not in {"gjs", "gjs-console"}:
        return None

    arguments = context.read_arguments(process)
    unknown = ApplicationMatch(process, group_same_executable=False)
    script = None

    for argument in arguments[1:]:

        if argument in {"-c", "--command", "--help", "--version"} or argument.startswith("--command="):
            return unknown

        if argument in {"-m", "--module", "--"}:
            continue

        if argument.startswith("-"):
            # Do not mistake an unknown option's argument for the script.
            return unknown

        script = argument
        break

    if not script or len(script) > 4000 or any(ord(char) < 32 for char in script):
        return unknown

    if script.startswith("resource:///"):
        location = script

    elif script.startswith("/"):
        location = f"{PurePosixPath(script)}"

    else:
        # A relative script is ambiguous without the interpreter's current directory.
        return unknown

    name = PurePosixPath(location).name[:512]

    if not name:
        return unknown

    return ApplicationMatch(process, ApplicationIdentity(ident=f"gjs:{location}", name=name))
