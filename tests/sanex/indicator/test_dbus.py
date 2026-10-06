"""Tests for StatusNotifierItem and DBusMenu adapters."""

from types import SimpleNamespace

import pytest
from dbus_fast import DBusError
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

    async def call(self, message: object) -> object:
        self.messages.append(message)
        self._fail("register")
        return SimpleNamespace(
            body=[],
            message_type=MessageType.METHOD_RETURN,
            error_name=None,
        )

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
    message = bus.messages[0]
    assert message.body == ["org.freedesktop.StatusNotifierItem-1234-1"]

    service.close()

    assert bus.unexported == [STATUS_ITEM_PATH, MENU_PATH]
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
    "step", ["connect", "instance_name", "item_name", f"export:{STATUS_ITEM_PATH}", f"export:{MENU_PATH}", "register"],
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
    service.close()


@pytest.mark.asyncio
async def test_close_unexports_every_interface_despite_error() -> None:
    bus = FakeBus()
    service = IndicatorDesktopService(IndicatorPresentationBuilder("1.2.3").build(None), lambda: None, bus_factory=lambda: bus)
    await service.start()
    bus.failures[f"unexport:{STATUS_ITEM_PATH}"] = OSError("unexport denied")

    with pytest.raises(OSError, match="unexport denied"):
        service.close()

    assert bus.unexported == [STATUS_ITEM_PATH, MENU_PATH]
    assert bus.disconnected
    service.close()
