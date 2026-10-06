"""StatusNotifierItem and DBusMenu adapters for the desktop session bus."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Protocol

from dbus_fast import DBusError, Message, Variant
from dbus_fast.annotations import (
    DBusBool,
    DBusInt32,
    DBusObjectPath,
    DBusSignature,
    DBusStr,
    DBusUInt32,
    DBusVariant,
)
from dbus_fast.aio import MessageBus
from dbus_fast.constants import NameFlag, PropertyAccess, RequestNameReply
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from ..exceptions import IndicatorError
from ..platform.dbus import DBUS_OPERATION_ERRORS, raise_dbus_error
from ..utils.resources import close_resources
from .presentation import IndicatorIcon, IndicatorPresentation


STATUS_ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/Menu"
STATUS_ITEM_INTERFACE = "org.kde.StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"
INDICATOR_INSTANCE_SERVICE = "org.sanecmp.SanexIndicator"

ROOT_ITEM = 0
STATUS_ITEM = 1
BREAK_ITEM = 2
SEPARATOR_ITEM = 3
ABOUT_ITEM = 4

IconPixmap = Annotated[list[tuple[int, int, bytes]], DBusSignature("a(iiay)")]
ToolTip = Annotated[
    tuple[str, list[tuple[int, int, bytes]], str, str],
    DBusSignature("(sa(iiay)ss)"),
]
MenuLayout = Annotated[
    tuple[int, dict[str, Variant], list[Variant]],
    DBusSignature("(ia{sv}av)"),
]
LayoutResponse = Annotated[
    tuple[int, tuple[int, dict[str, Variant], list[Variant]]],
    DBusSignature("u(ia{sv}av)"),
]
GroupProperties = Annotated[
    list[tuple[int, dict[str, Variant]]],
    DBusSignature("a(ia{sv})"),
]
MenuEvents = Annotated[
    list[tuple[int, str, Variant, int]],
    DBusSignature("a(isvu)"),
]
Int32Array = Annotated[list[int], DBusSignature("ai")]
UpdatedProperties = Annotated[
    list[tuple[int, dict[str, Variant]]],
    DBusSignature("a(ia{sv})"),
]
RemovedProperties = Annotated[
    list[tuple[int, list[str]]],
    DBusSignature("a(ias)"),
]


class SessionBus(Protocol):
    async def connect(self) -> object: ...

    async def request_name(
        self,
        name: str,
        flags: NameFlag = NameFlag.NONE,
    ) -> RequestNameReply: ...

    async def call(self, message: Message) -> Message: ...

    def export(self, path: str, interface: ServiceInterface) -> None: ...

    def unexport(
        self,
        path: str,
        interface: ServiceInterface | str | None = None,
    ) -> None: ...

    def disconnect(self) -> None: ...


@dataclass(slots=True)
class DbusMenuModel:
    """Maintain a small stable menu tree and dispatch its only action."""

    presentation: IndicatorPresentation
    open_about: Callable[[], None]
    revision: int = 1

    def update(self, presentation: IndicatorPresentation) -> bool:
        """Replace menu labels and increment the layout revision if changed."""

        if presentation == self.presentation:
            return False

        self.presentation = presentation
        self.revision += 1
        return True

    def build_layout(
        self,
        parent_ident: int,
        recursion_depth: int,
        property_names: list[str],
    ) -> tuple[int, tuple[int, dict[str, Variant], list[Variant]]]:
        """Build one DBusMenu layout response."""

        if parent_ident not in self.list_item_idents():
            self._raise_invalid_item(parent_ident)

        properties = self.get_properties(parent_ident, property_names)
        children = []

        if parent_ident == ROOT_ITEM and recursion_depth != 0:
            children = [
                Variant(
                    "(ia{sv}av)",
                    self._build_item_layout(ident, property_names),
                )
                for ident in self.list_child_idents()
            ]

        return self.revision, (parent_ident, properties, children)

    def list_item_idents(self) -> tuple[int, ...]:
        """Return all currently addressable menu item identities."""
        return (ROOT_ITEM, *self.list_child_idents())

    def list_child_idents(self) -> tuple[int, ...]:
        """Return visible root children in display order."""
        children = [STATUS_ITEM]

        if self.presentation.break_label is not None:
            children.append(BREAK_ITEM)

        children.extend((SEPARATOR_ITEM, ABOUT_ITEM))
        return tuple(children)

    def get_properties(
        self,
        ident: int,
        property_names: list[str],
    ) -> dict[str, Variant]:
        """Return filtered properties for one menu item."""
        presentation = self.presentation

        if ident == ROOT_ITEM:
            properties = {"children-display": Variant("s", "submenu")}

        elif ident == STATUS_ITEM:
            properties = {
                "label": Variant("s", presentation.status_label),
                "enabled": Variant("b", False),
            }

        elif ident == BREAK_ITEM and presentation.break_label is not None:
            properties = {
                "label": Variant("s", presentation.break_label),
                "enabled": Variant("b", False),
            }

        elif ident == SEPARATOR_ITEM:
            properties = {"type": Variant("s", "separator")}

        elif ident == ABOUT_ITEM:
            properties = {
                "label": Variant("s", presentation.about_label),
                "enabled": Variant("b", True),
            }

        else:
            self._raise_invalid_item(ident)

        if not property_names:
            return properties

        requested = set(property_names)
        return {
            name: value
            for name, value in properties.items()
            if name in requested
        }

    def activate(self, ident: int, event: str) -> bool:
        """Dispatch a supported click and report whether it was handled."""

        if ident not in self.list_item_idents():
            return False

        if ident == ABOUT_ITEM and event == "clicked":
            self.open_about()

        return True

    def _build_item_layout(
        self,
        ident: int,
        property_names: list[str],
    ) -> tuple[int, dict[str, Variant], list[Variant]]:
        return ident, self.get_properties(ident, property_names), []

    @staticmethod
    def _raise_invalid_item(ident: int) -> None:
        raise DBusError(
            "com.canonical.dbusmenu.Error.InvalidMenuItem",
            f"unknown menu item {ident}",
        )


class DbusMenuInterface(ServiceInterface):
    """Expose the presentation menu using com.canonical.dbusmenu."""

    def __init__(self, model: DbusMenuModel) -> None:
        super().__init__(MENU_INTERFACE)
        self.model = model

    @dbus_property(access=PropertyAccess.READ, name="Version")
    def get_version(self) -> DBusUInt32:
        return 3

    @dbus_property(access=PropertyAccess.READ, name="TextDirection")
    def get_text_direction(self) -> DBusStr:
        return "ltr"

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def get_status(self) -> DBusStr:
        return "normal"

    @dbus_property(access=PropertyAccess.READ, name="IconThemePath")
    def get_icon_theme_path(self) -> Annotated[list[str], DBusSignature("as")]:
        return []

    @method(name="GetLayout")
    def get_layout(
        self,
        parent_ident: DBusInt32,
        recursion_depth: DBusInt32,
        property_names: Annotated[list[str], DBusSignature("as")],
    ) -> LayoutResponse:
        return self.model.build_layout(
            parent_ident,
            recursion_depth,
            property_names,
        )

    @method(name="GetGroupProperties")
    def get_group_properties(
        self,
        idents: Int32Array,
        property_names: Annotated[list[str], DBusSignature("as")],
    ) -> GroupProperties:
        model = self.model
        known = set(model.list_item_idents())
        selected = model.list_item_idents() if not idents else tuple(idents)
        return [
            (ident, model.get_properties(ident, property_names))
            for ident in selected
            if ident in known
        ]

    @method(name="GetProperty")
    def get_property(self, ident: DBusInt32, name: DBusStr) -> DBusVariant:
        properties = self.model.get_properties(ident, [name])
        value = properties.get(name)

        if value is None:
            raise DBusError(
                "com.canonical.dbusmenu.Error.InvalidProperty",
                f"menu item {ident} has no property {name}",
            )

        return value

    @method(name="Event")
    def handle_event(
        self,
        ident: DBusInt32,
        event: DBusStr,
        data: DBusVariant,
        timestamp: DBusUInt32,
    ) -> None:
        self.model.activate(ident, event)

    @method(name="EventGroup")
    def handle_event_group(self, events: MenuEvents) -> Int32Array:
        return [
            ident
            for ident, event, data, timestamp in events
            if not self.model.activate(ident, event)
        ]

    @method(name="AboutToShow")
    def prepare_to_show(self, ident: DBusInt32) -> DBusBool:
        return False

    @method(name="AboutToShowGroup")
    def prepare_group_to_show(
        self,
        idents: Int32Array,
    ) -> Annotated[tuple[list[int], list[int]], DBusSignature("aiai")]:
        known = set(self.model.list_item_idents())
        return [], [ident for ident in idents if ident not in known]

    @signal(name="ItemsPropertiesUpdated")
    def notify_properties_updated(
        self,
        updated: UpdatedProperties,
        removed: RemovedProperties,
    ) -> Annotated[
        tuple[
            list[tuple[int, dict[str, Variant]]],
            list[tuple[int, list[str]]],
        ],
        DBusSignature("a(ia{sv})a(ias)"),
    ]:
        return updated, removed

    @signal(name="LayoutUpdated")
    def notify_layout_updated(
        self,
        revision: int,
        parent_ident: int,
    ) -> Annotated[tuple[int, int], DBusSignature("ui")]:
        return revision, parent_ident

    @signal(name="ItemActivationRequested")
    def request_item_activation(
        self,
        ident: int,
        timestamp: int,
    ) -> Annotated[tuple[int, int], DBusSignature("iu")]:
        return ident, timestamp

    def update(self, presentation: IndicatorPresentation) -> None:
        """Update menu labels and notify the host when they changed."""
        model = self.model

        if model.update(presentation):
            self.notify_layout_updated(model.revision, ROOT_ITEM)


class StatusNotifierItemInterface(ServiceInterface):
    """Expose icon, tooltip and menu location to a notifier host."""

    def __init__(self, presentation: IndicatorPresentation) -> None:
        super().__init__(STATUS_ITEM_INTERFACE)
        self.presentation = presentation

    @dbus_property(access=PropertyAccess.READ, name="Category")
    def get_category(self) -> DBusStr:
        return "SystemServices"

    @dbus_property(access=PropertyAccess.READ, name="Id")
    def get_ident(self) -> DBusStr:
        return "sanex"

    @dbus_property(access=PropertyAccess.READ, name="Title")
    def get_title(self) -> DBusStr:
        return "sanex"

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def get_status(self) -> DBusStr:

        if self.presentation.icon is IndicatorIcon.WARNING:
            return "NeedsAttention"

        return "Active"

    @dbus_property(access=PropertyAccess.READ, name="WindowId")
    def get_window_ident(self) -> DBusUInt32:
        return 0

    @dbus_property(access=PropertyAccess.READ, name="IconName")
    def get_icon_name(self) -> DBusStr:
        return f"{self.presentation.icon.value}"

    @dbus_property(access=PropertyAccess.READ, name="IconPixmap")
    def get_icon_pixmap(self) -> IconPixmap:
        return []

    @dbus_property(access=PropertyAccess.READ, name="AttentionIconName")
    def get_attention_icon_name(self) -> DBusStr:
        return f"{IndicatorIcon.WARNING.value}"

    @dbus_property(access=PropertyAccess.READ, name="AttentionIconPixmap")
    def get_attention_icon_pixmap(self) -> IconPixmap:
        return []

    @dbus_property(access=PropertyAccess.READ, name="OverlayIconName")
    def get_overlay_icon_name(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ, name="OverlayIconPixmap")
    def get_overlay_icon_pixmap(self) -> IconPixmap:
        return []

    @dbus_property(access=PropertyAccess.READ, name="ToolTip")
    def get_tooltip(self) -> ToolTip:
        presentation = self.presentation
        return (
            f"{presentation.icon.value}",
            [],
            "sanex",
            presentation.tooltip,
        )

    @dbus_property(access=PropertyAccess.READ, name="ItemIsMenu")
    def get_item_is_menu(self) -> DBusBool:
        return True

    @dbus_property(access=PropertyAccess.READ, name="Menu")
    def get_menu(self) -> DBusObjectPath:
        return MENU_PATH

    @method(name="ContextMenu")
    def open_context_menu(self, position_x: DBusInt32, position_y: DBusInt32) -> None:
        return None

    @method(name="Activate")
    def activate(self, position_x: DBusInt32, position_y: DBusInt32) -> None:
        return None

    @method(name="SecondaryActivate")
    def activate_secondary(self, position_x: DBusInt32, position_y: DBusInt32) -> None:
        return None

    @method(name="Scroll")
    def scroll(self, delta: DBusInt32, orientation: DBusStr) -> None:
        return None

    @signal(name="NewIcon")
    def notify_new_icon(self) -> None:
        return None

    @signal(name="NewAttentionIcon")
    def notify_new_attention_icon(self) -> None:
        return None

    @signal(name="NewToolTip")
    def notify_new_tooltip(self) -> None:
        return None

    @signal(name="NewStatus")
    def notify_new_status(self, status: str) -> DBusStr:
        return status

    def update(self, presentation: IndicatorPresentation) -> None:
        """Replace visible properties and emit the required change signals."""
        previous = self.presentation

        if presentation == previous:
            return

        self.presentation = presentation

        if presentation.icon != previous.icon:
            self.notify_new_icon()
            self.notify_new_attention_icon()
            self.notify_new_status(self.get_status)

        if presentation.tooltip != previous.tooltip or presentation.icon != previous.icon:
            self.notify_new_tooltip()


@dataclass(slots=True)
class IndicatorDesktopService:
    """Own session-bus exports for one desktop indicator process."""

    presentation: IndicatorPresentation
    open_about: Callable[[], None]
    process_ident: int = field(default_factory=os.getpid)
    bus_factory: Callable[[], SessionBus] = MessageBus
    _bus: SessionBus | None = field(default=None, init=False, repr=False)
    _item: StatusNotifierItemInterface = field(init=False, repr=False)
    _menu: DbusMenuInterface = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._item = StatusNotifierItemInterface(self.presentation)
        self._menu = DbusMenuInterface(
            DbusMenuModel(self.presentation, self.open_about)
        )

    async def start(self) -> bool:
        """Register the item, or report that this session already has one."""

        if self._bus is not None:
            return True

        bus = self.bus_factory()
        exported_paths: list[str] = []
        transferred = False
        service_name = f"org.freedesktop.StatusNotifierItem-{self.process_ident}-1"
        try:
            await bus.connect()
            instance_reply = await bus.request_name(
                INDICATOR_INSTANCE_SERVICE,
                NameFlag.DO_NOT_QUEUE,
            )

            if instance_reply in (
                RequestNameReply.EXISTS,
                RequestNameReply.IN_QUEUE,
            ):
                return False

            if instance_reply not in (
                RequestNameReply.PRIMARY_OWNER,
                RequestNameReply.ALREADY_OWNER,
            ):
                raise IndicatorError(
                    "request indicator instance D-Bus name",
                    f"unexpected reply {instance_reply}",
                )

            reply = await bus.request_name(service_name)

            if reply not in (
                RequestNameReply.PRIMARY_OWNER,
                RequestNameReply.ALREADY_OWNER,
            ):
                raise IndicatorError("request D-Bus name", f"unexpected reply {reply}")

            exported_paths.append(STATUS_ITEM_PATH)
            bus.export(STATUS_ITEM_PATH, self._item)
            exported_paths.append(MENU_PATH)
            bus.export(MENU_PATH, self._menu)
            response = await bus.call(
                Message(
                    destination=WATCHER_SERVICE,
                    path=WATCHER_PATH,
                    interface=WATCHER_INTERFACE,
                    member="RegisterStatusNotifierItem",
                    signature="s",
                    body=[service_name],
                )
            )
            raise_dbus_error(response)
            self._bus = bus
            transferred = True
            return True

        except DBUS_OPERATION_ERRORS as error:
            raise IndicatorError("register desktop item", error) from error

        finally:

            if not transferred:
                close_resources(
                    *(("unexport desktop interface", lambda path=path: bus.unexport(path)) for path in exported_paths),
                    ("disconnect desktop bus", bus.disconnect),
                )

    def update(self, presentation: IndicatorPresentation) -> None:
        """Apply one presentation update to both exported interfaces."""

        if presentation == self.presentation:
            return

        self.presentation = presentation
        self._item.update(presentation)
        self._menu.update(presentation)

    def close(self) -> None:
        """Unexport the item and disconnect from the session bus."""
        bus = self._bus
        self._bus = None

        if bus is None:
            return

        close_resources(
            ("unexport status item", lambda: bus.unexport(STATUS_ITEM_PATH)),
            ("unexport menu", lambda: bus.unexport(MENU_PATH)),
            ("disconnect desktop bus", bus.disconnect),
        )
