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

    with pytest.raises(ValidationError):
        session.active = False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ident", ""),
        ("uid", True),
        ("started", -1),
        ("type", "tty"),
        ("state", "offline"),
    ],
)
def test_login_session_rejects_invalid_values(field: str, value: object) -> None:
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

    with pytest.raises(ValidationError):
        LoginSession.model_validate(payload)
