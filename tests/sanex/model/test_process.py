"""Tests for stable Linux process identities."""

from typing import Any

import pytest
from pydantic import ValidationError

from sanex.model.process import ProcessIdentity


def test_process_identity_is_strict_and_frozen(process_payload: dict[str, Any]) -> None:
    process = ProcessIdentity.model_validate(
        {
            key: process_payload[key]
            for key in ("pid", "parent_pid", "uid", "started", "prc_name", "exe")
        }
    )

    assert process.started == 5_753_094
    assert process.parent_pid == 1

    with pytest.raises(ValidationError):
        process.pid = 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", 0),
        ("parent_pid", -1),
        ("parent_pid", None),
        ("uid", -1),
        ("started", -1),
        ("prc_name", ""),
        ("exe", ""),
    ],
)
def test_process_identity_rejects_invalid_values(
    process_payload: dict[str, Any],
    field: str,
    value: object,
) -> None:
    values = {
        key: process_payload[key]
        for key in ("pid", "parent_pid", "uid", "started", "prc_name", "exe")
    }
    values[field] = value

    with pytest.raises(ValidationError):
        ProcessIdentity.model_validate(values)


def test_process_identity_requires_parent_pid(process_payload: dict[str, Any]) -> None:
    values = {
        key: process_payload[key]
        for key in ("pid", "uid", "started", "prc_name", "exe")
    }

    with pytest.raises(ValidationError, match=r"parent_pid\s+Field required"):
        ProcessIdentity.model_validate(values)
