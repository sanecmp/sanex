"""Tests for StatusNotifierItem and DBusMenu adapters."""

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

import pytest
import pytest_asyncio
from dbus_fast import DBusError, Message, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import MessageType, NameFlag, RequestNameReply

from sanex.indicator.dbus import (
    ABOUT_ITEM,
    BREAK_ITEM,
    INDICATOR_INSTANCE_SERVICE,
    MENU_INTERFACE,
    MENU_PATH,
    ROOT_ITEM,
    SEPARATOR_ITEM,
    STATUS_ITEM,
    STATUS_ITEM_INTERFACE,
    STATUS_ITEM_PATH,
    WATCHER_SERVICE,
    DbusMenuInterface,
    DbusMenuModel,
    IndicatorDesktopService,
    StatusNotifierItemInterface,
)
from sanex.indicator.presentation import IndicatorPresentationBuilder
from sanex.model.indicator import IndicatorStatus

import asyncio
from sanex.exceptions import IndicatorError


class FakeBus:
    def __init__(
        self,
        instance_reply: RequestNameReply = RequestNameReply.PRIMARY_OWNER,
    ) -> None:
        self.failures: dict[str, BaseException] = {}
        self.instance_reply = instance_reply
        self.connected = False
        self.disconnected = False
        self.requested_names: list[tuple[str, NameFlag]] = []
        self.exports: list[tuple[str, object]] = []
        self.unexported: list[str] = []
        self.messages: list[object] = []
        self.handlers: list[Callable[[Message], object]] = []
        self.watcher_error: str | None = None
        self.registered = asyncio.Event()

    async def connect(self) -> object:
        self.connected = True
        self._fail("connect")
        return self

    async def request_name(
        self,
        name: str,
        flags: NameFlag = NameFlag.NONE,
    ) -> RequestNameReply:
        self.requested_names.append((name, flags))
        self._fail("instance_name" if name == INDICATOR_INSTANCE_SERVICE else "item_name")

        if name == INDICATOR_INSTANCE_SERVICE:
            return self.instance_reply

        return RequestNameReply.PRIMARY_OWNER

    async def call(self, message: Message) -> object:
        self.messages.append(message)
        self._fail("match" if message.member == "AddMatch" else "register")

        if message.member == "RegisterStatusNotifierItem":
            self.registered.set()

            if self.watcher_error is not None:
                return SimpleNamespace(
                    body=["The watcher is unavailable"],
                    message_type=MessageType.ERROR,
                    error_name=self.watcher_error,
                )

        return SimpleNamespace(
            body=[],
            message_type=MessageType.METHOD_RETURN,
            error_name=None,
        )

    def add_message_handler(self, handler: Callable[[Message], object]) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler: Callable[[Message], object]) -> None:
        self.handlers.remove(handler)

    def emit_owner_change(
        self, previous: str, current: str, *, name: str = WATCHER_SERVICE, sender: str = "org.freedesktop.DBus",
    ) -> None:
        message = Message(
            message_type=MessageType.SIGNAL,
            sender=sender,
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="NameOwnerChanged",
            signature="sss",
            body=[name, previous, current],
        )

        for handler in tuple(self.handlers):
            handler(message)

    def export(self, path: str, interface: object) -> None:
        self.exports.append((path, interface))
        self._fail(f"export:{path}")

    def unexport(self, path: str, interface: object = None) -> None:
        self.unexported.append(path)
        self._fail(f"unexport:{path}")

    def disconnect(self) -> None:
        self.disconnected = True
        self._fail("disconnect")

    def _fail(self, step: str) -> None:

        if failure := self.failures.get(step):
            raise failure


@pytest_asyncio.fixture
async def menu_signal_bus(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[MessageBus, list[Message]]]:
    """Capture outbound signals without connecting to the desktop session bus."""
    bus = MessageBus(bus_address="unix:path=/sanex-test-unused")
    messages: list[Message] = []
    monkeypatch.setattr(MessageBus, "send", lambda _bus, message: messages.append(message))
    try:
        yield bus, messages

    finally:
        bus.unexport(MENU_PATH)
        bus.disconnect()


def test_builds_stable_menu_and_dispatches_about_action() -> None:
    opened: list[bool] = []
    presentation = IndicatorPresentationBuilder("1.2.3").build(
        IndicatorStatus(remaining=3600, break_duration=7200)
    )
    model = DbusMenuModel(presentation, lambda: opened.append(True))

    revision, layout = model.build_layout(ROOT_ITEM, -1, [])
    child_idents = [child.value[0] for child in layout[2]]

    assert revision == 1
    assert child_idents == [STATUS_ITEM, BREAK_ITEM, SEPARATOR_ITEM, ABOUT_ITEM]
    assert model.get_properties(STATUS_ITEM, ["label"])["label"].value == (
        "Time remaining: 1 h"
    )
    assert model.activate(ABOUT_ITEM, "clicked")
    assert opened == [True]


@pytest.mark.parametrize(
    "previous_status, status, expected_labels",
    [
        pytest.param(None, IndicatorStatus(remaining=None, break_duration=0), {STATUS_ITEM: "No time limit"}, id="reconnected"),
        pytest.param(
            IndicatorStatus(remaining=None, break_duration=0), None,
            {STATUS_ITEM: "Sanex service is unavailable"}, id="disconnected",
        ),
        pytest.param(
            IndicatorStatus(remaining=3600, break_duration=7200), IndicatorStatus(remaining=3540, break_duration=7140),
            {STATUS_ITEM: "Time remaining: 59 min", BREAK_ITEM: "Required break: 1 h 59 min"}, id="durations",
        ),
    ],
)
@pytest.mark.asyncio
async def test_updates_cached_menu_labels_without_rebuilding_layout(
    menu_signal_bus: tuple[MessageBus, list[Message]],
    previous_status: IndicatorStatus | None,
    status: IndicatorStatus | None,
    expected_labels: dict[int, str],
) -> None:
    bus, messages = menu_signal_bus
    builder = IndicatorPresentationBuilder("1.2.3")
    menu = DbusMenuInterface(DbusMenuModel(builder.build(previous_status), lambda: None))
    bus.export(MENU_PATH, menu)
    messages.clear()
    presentation = builder.build(status)

    menu.update(presentation)
    menu.update(presentation)

    assert len(messages) == 1
    message = messages[0]
    assert message.path == MENU_PATH
    assert message.interface == MENU_INTERFACE
    assert message.member == "ItemsPropertiesUpdated"
    assert message.signature == "a(ia{sv})a(ias)"
    assert message.body == [
        [(ident, {"label": Variant("s", label), "enabled": Variant("b", False)}) for ident, label in expected_labels.items()],
        [],
    ]


@pytest.mark.parametrize("break_duration", [0, 60], ids=["break-added", "break-removed"])
@pytest.mark.asyncio
async def test_updates_existing_labels_when_break_item_is_added_or_removed(
    menu_signal_bus: tuple[MessageBus, list[Message]], break_duration: int,
) -> None:
    bus, messages = menu_signal_bus
    builder = IndicatorPresentationBuilder("1.2.3")
    previous = builder.build(IndicatorStatus(remaining=3600, break_duration=break_duration))
    menu = DbusMenuInterface(DbusMenuModel(previous, lambda: None))
    bus.export(MENU_PATH, menu)
    messages.clear()
    presentation = builder.build(IndicatorStatus(remaining=3540, break_duration=60 - break_duration))

    menu.update(presentation)

    assert [message.member for message in messages] == ["ItemsPropertiesUpdated", "LayoutUpdated"]
    assert messages[0].body == [
        [(STATUS_ITEM, {"label": Variant("s", "Time remaining: 59 min"), "enabled": Variant("b", False)})], [],
    ]
    assert messages[1].signature == "ui"
    assert messages[1].body == [2, ROOT_ITEM]
    children = menu.model.build_layout(ROOT_ITEM, -1, [])[1][2]
    assert [child.value[0] for child in children] == list(menu.model.list_child_idents())
    assert (BREAK_ITEM in menu.model.list_child_idents()) == (presentation.break_label is not None)


@pytest.mark.parametrize(
    "break_duration, expected_layouts",
    [pytest.param(0, [], id="unchanged"), pytest.param(60, [[2, ROOT_ITEM]], id="break-added")],
)
@pytest.mark.asyncio
async def test_emits_only_layout_update_when_menu_labels_are_unchanged(
    menu_signal_bus: tuple[MessageBus, list[Message]], break_duration: int, expected_layouts: list[list[int]],
) -> None:
    bus, messages = menu_signal_bus
    builder = IndicatorPresentationBuilder("1.2.3")
    previous = builder.build(IndicatorStatus(remaining=None, break_duration=0))
    menu = DbusMenuInterface(DbusMenuModel(previous, lambda: None))
    bus.export(MENU_PATH, menu)
    messages.clear()

    menu.update(builder.build(IndicatorStatus(remaining=None, break_duration=break_duration)))

    assert [(message.member, message.signature, message.body) for message in messages] == [
        ("LayoutUpdated", "ui", body) for body in expected_layouts
    ]


def test_omits_zero_break_and_rejects_unknown_item() -> None:
    presentation = IndicatorPresentationBuilder("1.2.3").build(
        IndicatorStatus(remaining=None, break_duration=0)
    )
    model = DbusMenuModel(presentation, lambda: None)

    assert BREAK_ITEM not in model.list_child_idents()

    with pytest.raises(DBusError, match="unknown menu item"):
        model.get_properties(BREAK_ITEM, [])


def test_exposes_expected_dbus_interfaces_and_properties() -> None:
    presentation = IndicatorPresentationBuilder("1.2.3").build(None)
    menu = DbusMenuInterface(DbusMenuModel(presentation, lambda: None))
    item = StatusNotifierItemInterface(presentation)

    menu_introspection = menu.introspect()
    item_introspection = item.introspect()

    assert menu_introspection.name == MENU_INTERFACE
    assert {entry.name for entry in menu_introspection.methods} >= {
        "GetLayout",
        "Event",
        "AboutToShow",
    }
    assert item_introspection.name == STATUS_ITEM_INTERFACE
    assert {entry.name for entry in item_introspection.properties} >= {
        "IconName",
        "Status",
        "ToolTip",
        "Menu",
    }
    methods = {entry.name: entry for entry in item_introspection.methods}

    for name in ("ContextMenu", "Activate", "SecondaryActivate"):
        assert methods[name].in_signature == "ii"
        assert [argument.name for argument in methods[name].in_args] == ["position_x", "position_y"]

    assert item.get_status == "NeedsAttention"
    assert item.get_icon_name == "sanex-warning-symbolic"
    assert item.get_menu == MENU_PATH


@pytest.mark.asyncio
async def test_registers_and_unexports_desktop_item() -> None:
    bus = FakeBus()
    presentation = IndicatorPresentationBuilder("1.2.3").build(None)
    service = IndicatorDesktopService(
        presentation=presentation,
        open_about=lambda: None,
        process_ident=1234,
        bus_factory=lambda: bus,
    )

    assert await service.start()

    assert bus.connected
    assert bus.requested_names == [
        (INDICATOR_INSTANCE_SERVICE, NameFlag.DO_NOT_QUEUE),
        ("org.freedesktop.StatusNotifierItem-1234-1", NameFlag.NONE),
    ]
    assert [path for path, interface in bus.exports] == [
        STATUS_ITEM_PATH,
        MENU_PATH,
    ]
    assert bus.messages[0].member == "AddMatch"
    assert f"arg0='{WATCHER_SERVICE}'" in bus.messages[0].body[0]
    message = bus.messages[1]
    assert message.body == ["org.freedesktop.StatusNotifierItem-1234-1"]

    await service.close()

    assert bus.unexported == [STATUS_ITEM_PATH, MENU_PATH]
    assert bus.disconnected
    assert bus.handlers == []


@pytest.mark.parametrize("error_name", ["ServiceUnknown", "NameHasNoOwner"])
@pytest.mark.asyncio
async def test_waits_for_watcher_started_after_indicator(error_name: str) -> None:
    bus = FakeBus()
    bus.watcher_error = f"org.freedesktop.DBus.Error.{error_name}"
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)

    assert await service.start()
    assert not bus.disconnected
    bus.registered.clear()
    bus.watcher_error = None
    bus.emit_owner_change("", ":1.42")
    await bus.registered.wait()

    assert [message.member for message in bus.messages] == [
        "AddMatch", "RegisterStatusNotifierItem", "RegisterStatusNotifierItem",
    ]
    await service.close()
    assert bus.handlers == []
    assert bus.disconnected


@pytest.mark.asyncio
async def test_registers_again_after_watcher_restart() -> None:
    bus = FakeBus()
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)
    assert await service.start()
    bus.registered.clear()

    bus.emit_owner_change(":1.42", "")
    bus.emit_owner_change("", ":1.55", name="org.example.Unrelated")
    bus.emit_owner_change("", ":1.56", sender=":1.99")
    await asyncio.sleep(0)
    assert not bus.registered.is_set()
    bus.emit_owner_change("", ":1.43")
    await bus.registered.wait()

    registrations = [message for message in bus.messages if message.member == "RegisterStatusNotifierItem"]
    assert len(registrations) == 2
    assert registrations[0].body == registrations[1].body
    await service.close()


@pytest.mark.asyncio
async def test_does_not_miss_watcher_arriving_during_registration() -> None:
    bus = FakeBus()
    bus.watcher_error = "org.freedesktop.DBus.Error.ServiceUnknown"
    call = bus.call

    async def register_during_call(message: Message) -> object:
        response = await call(message)

        if message.member == "RegisterStatusNotifierItem" and bus.watcher_error is not None:
            bus.watcher_error = None
            bus.registered.clear()
            bus.emit_owner_change("", ":1.42")

        return response

    bus.call = register_during_call
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)

    assert await service.start()
    await bus.registered.wait()

    assert [message.member for message in bus.messages].count("RegisterStatusNotifierItem") == 2
    await service.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_watcher_registration() -> None:
    bus = FakeBus()
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)
    assert await service.start()
    pending = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_for_registration(message: Message) -> object:
        pending.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bus.call = wait_for_registration
    bus.emit_owner_change(":1.42", ":1.43")
    await pending.wait()

    await service.close()

    assert cancelled.is_set()
    assert bus.handlers == []
    assert bus.unexported == [STATUS_ITEM_PATH, MENU_PATH]
    assert bus.disconnected


@pytest.mark.asyncio
async def test_does_not_hide_unexpected_watcher_error() -> None:
    bus = FakeBus()
    bus.watcher_error = "org.freedesktop.DBus.Error.AccessDenied"
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)

    with pytest.raises(IndicatorError, match="AccessDenied"):
        await service.start()

    assert bus.handlers == []
    assert bus.disconnected


@pytest.mark.asyncio
async def test_stops_when_indicator_already_runs_in_session() -> None:
    bus = FakeBus(RequestNameReply.EXISTS)
    presentation = IndicatorPresentationBuilder("1.2.3").build(None)
    service = IndicatorDesktopService(
        presentation=presentation,
        open_about=lambda: None,
        bus_factory=lambda: bus,
    )

    assert not await service.start()
    assert bus.requested_names == [
        (INDICATOR_INSTANCE_SERVICE, NameFlag.DO_NOT_QUEUE),
    ]
    assert bus.exports == []
    assert bus.messages == []
    assert bus.disconnected


def test_updates_item_and_menu_together() -> None:
    builder = IndicatorPresentationBuilder("1.2.3")
    service = IndicatorDesktopService(
        presentation=builder.build(None),
        open_about=lambda: None,
    )
    normal = builder.build(IndicatorStatus(remaining=None, break_duration=0))

    service.update(normal)

    assert service.presentation == normal
    assert service._item.get_status == "Active"
    assert service._item.get_icon_name == "sanex-symbolic"
    assert service._menu.model.presentation == normal


@pytest.mark.parametrize(
    "step", ["connect", "instance_name", "item_name", f"export:{STATUS_ITEM_PATH}", f"export:{MENU_PATH}", "match", "register"],
)
@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_start_releases_partial_registration(step: str, cancel: bool) -> None:

    bus = FakeBus()
    bus.failures[step] = asyncio.CancelledError("registration interrupted") if cancel else OSError("registration interrupted")
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)
    expected = asyncio.CancelledError if cancel else IndicatorError

    with pytest.raises(expected, match="registration interrupted"):
        await service.start()

    assert bus.disconnected
    assert bus.unexported == [path for path, interface in bus.exports]
    assert bus.handlers == []
    await service.close()


@pytest.mark.asyncio
async def test_close_unexports_every_interface_despite_error() -> None:
    bus = FakeBus()
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)
    await service.start()
    bus.failures[f"unexport:{STATUS_ITEM_PATH}"] = OSError("unexport denied")

    with pytest.raises(OSError, match="unexport denied"):
        await service.close()

    assert bus.unexported == [STATUS_ITEM_PATH, MENU_PATH]
    assert bus.disconnected
    assert bus.handlers == []
    await service.close()
