"""Tests for immutable logind session snapshots."""

from typing import Any

import pytest
from pydantic import ValidationError

from sanex.model.session import LoginSession, SessionState, SessionType


def test_login_session_is_strict_typed_and_frozen(session_payload: list[dict[str, Any]]) -> None:
    sample = session_payload[0]

    session = LoginSession(
        ident=sample["ident"],
        uid=sample["uid"],
        login=sample["login"],
        path=sample["path"],
        started=sample["timestamp_us"] // 1_000_000,
        type=SessionType(sample["type"]),
        state=SessionState(sample["state"]),
        active=sample["active"],
        idle=sample["idle"],
        locked=sample["locked"],
    )

    assert session.type is SessionType.WAYLAND
    assert session.state is SessionState.ACTIVE

    with pytest.raises(ValidationError, match="Instance is frozen"):
        session.active = False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param("ident", "", "String should have at least 1 character", id="empty-ident"),
        pytest.param("uid", True, "Input should be a valid integer", id="boolean-uid"),
        pytest.param("started", -1, "Input should be greater than or equal to 0", id="negative-start"),
        pytest.param("type", "tty", "Input should be an instance of SessionType", id="untyped-session-type"),
        pytest.param("state", "offline", "Input should be an instance of SessionState", id="untyped-session-state"),
    ],
)
def test_login_session_rejects_invalid_values(field: str, value: object, message: str) -> None:
    payload = {
        "ident": "2",
        "uid": 1001,
        "login": "child",
        "path": "/org/freedesktop/login1/session/_32",
        "started": 1,
        "type": SessionType.WAYLAND,
        "state": SessionState.ACTIVE,
        "active": True,
        "idle": False,
        "locked": False,
    }
    payload[field] = value

    with pytest.raises(ValidationError, match=rf"{field}\s+{message}"):
        LoginSession.model_validate(payload)
