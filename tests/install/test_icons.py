"""Tests for installed symbolic indicator icons."""

from pathlib import Path
from xml.etree import ElementTree

import pytest


PROJECT_ROOT = Path(__file__).parents[2]
ICON_DIRECTORY = PROJECT_ROOT / "icons" / "hicolor" / "scalable" / "status"
ICON_PATHS = (
    ICON_DIRECTORY / "sanex-symbolic.svg",
    ICON_DIRECTORY / "sanex-warning-symbolic.svg",
)
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


@pytest.mark.parametrize("path", ICON_PATHS)
def test_symbolic_icon_is_valid_small_monochrome_svg(path: Path) -> None:
    root = ElementTree.parse(path).getroot()
    colors = {
        value.lower()
        for element in root.iter()
        for name, value in element.attrib.items()
        if name in {"fill", "stroke"} and value != "none"
    }

    assert root.tag == f"{SVG_NAMESPACE}svg"
    assert root.attrib["width"] == "16"
    assert root.attrib["height"] == "16"
    assert root.attrib["viewBox"] == "0 0 16 16"
    assert root.find(f"{SVG_NAMESPACE}title") is not None
    assert colors == {"#2e3436"}


def test_normal_and_warning_icons_are_distinct() -> None:
    normal, warning = ICON_PATHS

    assert normal.read_bytes() != warning.read_bytes()
