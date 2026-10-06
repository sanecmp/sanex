"""Shared low-level platform test doubles."""

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

    async def connect(self) -> object:
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
        self._fail("disconnect")

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
