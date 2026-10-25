"""Shared low-level platform test doubles."""

import asyncio
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace

from dbus_fast.constants import MessageType


class FakeBus:
    """Minimal ordered system-bus double."""

    def __init__(self, responses: list[object], failures: dict[str, BaseException] | None = None) -> None:
        self.failures = failures or {}
        self.disconnect_count = 0
        self.responses = deque(responses)
        self.messages: list[object] = []
        self.connected = False
        self.handlers: list[Callable[[object], object]] = []
        self.disconnect_wait_count = 0
        self._disconnect_future: asyncio.Future[None] | None = None

    async def connect(self) -> object:
        self._disconnect_future = asyncio.get_running_loop().create_future()
        self.connected = True
        self._fail("connect")
        return self

    async def call(self, message: object) -> object:
        self.messages.append(message)
        self._fail(message.member)
        response = self.responses.popleft()

        if isinstance(response, BaseException):
            raise response

        return response

    def add_message_handler(self, handler: Callable[[object], object]) -> None:
        self.handlers.append(handler)
        self._fail("add_handler")

    def remove_message_handler(self, handler: Callable[[object], object]) -> None:
        self._fail("remove_handler")

        if handler in self.handlers:
            self.handlers.remove(handler)

    def emit(self, message: object) -> None:

        for handler in tuple(self.handlers):
            handler(message)

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self.connected = False

        if self._disconnect_future is not None and not self._disconnect_future.done():
            self._disconnect_future.set_result(None)

        self._fail("disconnect")

    async def wait_for_disconnect(self) -> None:
        self.disconnect_wait_count += 1
        assert self._disconnect_future is not None
        await self._disconnect_future

    def drop(self, error: BaseException) -> None:
        """Simulate an unsolicited peer disconnect, retaining its exception."""
        self.connected = False
        assert self._disconnect_future is not None
        self._disconnect_future.set_exception(error)

    def _fail(self, operation: str) -> None:

        if error := self.failures.get(operation):
            raise error


def dbus_reply(body: list[object]) -> object:
    """Build one successful D-Bus reply double."""
    return SimpleNamespace(body=body, message_type=MessageType.METHOD_RETURN, error_name=None)


def dbus_error(name: str, detail: str) -> object:
    """Build one failed D-Bus reply double."""
    return SimpleNamespace(
        body=[detail],
        message_type=MessageType.ERROR,
        error_name=name,
    )
