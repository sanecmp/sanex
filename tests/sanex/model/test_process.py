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

    with pytest.raises(ValidationError, match="Instance is frozen"):
        process.pid = 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param("pid", 0, "Input should be greater than 0", id="zero-pid"),
        pytest.param("parent_pid", -1, "Input should be greater than or equal to 0", id="negative-parent-pid"),
        pytest.param("parent_pid", None, "Input should be a valid integer", id="null-parent-pid"),
        pytest.param("uid", -1, "Input should be greater than or equal to 0", id="negative-uid"),
        pytest.param("started", -1, "Input should be greater than or equal to 0", id="negative-start"),
        pytest.param("prc_name", "", "String should have at least 1 character", id="empty-name"),
        pytest.param("exe", "", "String should have at least 1 character", id="empty-executable"),
    ],
)
def test_process_identity_rejects_invalid_values(
    process_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
) -> None:
    values = {
        key: process_payload[key]
        for key in ("pid", "parent_pid", "uid", "started", "prc_name", "exe")
    }
    values[field] = value

    with pytest.raises(ValidationError, match=rf"{field}\s+{message}"):
        ProcessIdentity.model_validate(values)


def test_process_identity_requires_parent_pid(process_payload: dict[str, Any]) -> None:
    values = {
        key: process_payload[key]
        for key in ("pid", "uid", "started", "prc_name", "exe")
    }

    with pytest.raises(ValidationError, match=r"parent_pid\s+Field required"):
        ProcessIdentity.model_validate(values)
