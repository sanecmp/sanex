"""On-demand About dialog kept outside the idle indicator process."""

import asyncio
import html
import logging
from dataclasses import dataclass, field

from .presentation import AboutPresentation, IndicatorIcon
from ..utils.resources import close_async_resources


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AboutDialog:
    """Open at most one short-lived native Zenity information dialog."""

    presentation: AboutPresentation
    executable: str = "zenity"
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _process: asyncio.subprocess.Process | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def open(self) -> None:
        """Schedule the dialog without blocking D-Bus event handling."""
        task = self._task

        if task is not None and not task.done():
            return

        self._task = asyncio.create_task(self._show(), name="sanex-about")

    async def close(self) -> None:
        """Close the dialog and reap its helper process during shutdown."""
        task = self._task

        if task is not None:
            task.cancel()

            result, = await asyncio.gather(task, return_exceptions=True)

            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

        self._task = None

    async def _show(self) -> None:
        presentation = self.presentation
        project_url = html.escape(presentation.project_url, quote=True)
        project_label = html.escape(presentation.project_label)
        version = html.escape(presentation.version)
        text = f"{version}\n\n<a href=\"{project_url}\">{project_label}</a>"
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "--info",
                f"--title={presentation.title}",
                f"--text={text}",
                f"--icon={IndicatorIcon.NORMAL.value}",
                "--no-wrap",
            )
            self._process = process
            await process.wait()

        except OSError as error:
            logger.warning("Unable to open the sanex About dialog: %s", error)

        finally:
            process = self._process

            try:

                if process is not None:
                    operations = []

                    if process.returncode is None:
                        operations.append(("terminate About process", process.terminate))

                    operations.append(("wait for About process", process.wait))
                    await close_async_resources(*operations)

            finally:
                self._process = None
