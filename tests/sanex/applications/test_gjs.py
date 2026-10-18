"""Public window attribution for applications sharing GJS."""

from collections.abc import Callable

import pytest

from sanex.model.process import ProcessIdentity
from sanex.model.window import AtspiWindow
from sanex.platform.process import WindowProcessResolver


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("gjs", "/usr/share/desktop-icons/ding.js"), "gjs:/usr/share/desktop-icons/ding.js"),
        (("gjs", "-m", "/apps/maps.js", "--service"), "gjs:/apps/maps.js"),
        (("gjs", "--module", "resource:///org/example/main.js"), "gjs:resource:///org/example/main.js"),
        (("gjs", "-c", "print('synthetic')"), None),
        (("gjs", "--command=synthetic"), None),
        (("gjs", "--unknown", "/not-a-script"), None),
        (("gjs", "relative.js"), None),
        (("gjs", "/"), None),
        ((), None),
    ],
)
def test_identifies_script_without_guessing_desktop_component(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow],
    resolver: WindowProcessResolver, arguments: tuple[str, ...], expected: str | None,
) -> None:
    process = process_create(exe="/usr/bin/gjs-console", name="gjs-console", arguments=arguments)

    group = resolver.resolve((window_create(),), 1001)[0]

    assert (group.application.ident if group.application else None) == expected
    assert group.process == process
    assert group.owners == (process,)


def test_does_not_merge_different_scripts_even_in_one_interpreter_tree(
    process_create: Callable[..., ProcessIdentity], window_create: Callable[..., AtspiWindow], resolver: WindowProcessResolver,
) -> None:
    process_create(exe="/usr/bin/gjs-console", arguments=("gjs", "/apps/first.js"))
    child = process_create(pid=101, parent_pid=100, exe="/usr/bin/gjs-console", arguments=("gjs", "/apps/second.js"))

    groups = resolver.resolve((window_create(), window_create(pid=101)), 1001)

    assert [group.process.pid for group in groups] == [100, child.pid]
    assert [group.application.ident for group in groups] == ["gjs:/apps/first.js", "gjs:/apps/second.js"]
