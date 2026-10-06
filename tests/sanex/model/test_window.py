"""Tests for immutable AT-SPI window snapshots."""

from typing import Any

import pytest
from pydantic import ValidationError

from sanex.model.window import AtspiWindow, WindowRole


def test_window_snapshot_is_strict_and_frozen(atspi_payload: dict[str, Any]) -> None:
    sample = atspi_payload["children"][0]
    window = AtspiWindow(
        bus=sample["bus"],
        path=sample["path"],
        pid=sample["pid"],
        title=sample["title"],
        role=WindowRole(sample["role"]),
    )

    assert window.role is WindowRole.FRAME

    with pytest.raises(ValidationError):
        window.title = "Other"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bus", ""),
        ("pid", 0),
        ("role", 23),
    ],
)
def test_window_snapshot_rejects_invalid_values(
    atspi_payload: dict[str, Any],
    field: str,
    value: object,
) -> None:
    sample = atspi_payload["children"][0]
    values = {
        "bus": sample["bus"],
        "path": sample["path"],
        "pid": sample["pid"],
        "title": sample["title"],
        "role": WindowRole(sample["role"]),
    }
    values[field] = value

    with pytest.raises(ValidationError):
        AtspiWindow.model_validate(values)
