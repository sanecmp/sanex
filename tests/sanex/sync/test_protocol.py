"""Tests for strict synchronization control messages."""

import json
from typing import Any

import pytest
from sanelib.protocol import encode_sync_response

from sanex.exceptions import SyncProtocolError
from sanex.sync.protocol import parse_sync_response


def test_control_response_round_trip(sync_payload: dict[str, Any]) -> None:
    response = parse_sync_response(json.dumps(sync_payload["response"]))

    assert parse_sync_response(encode_sync_response(response)) == response
    assert response.commands[0].payload["version"] == "0.2.0.dev3+g4f81a2c"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("unexpected", True, "Extra inputs are not permitted"),
        ("commands", [{"ident": 1, "type": "x", "payload": []}], "object"),
    ],
)
def test_invalid_control_responses_are_rejected(
    sync_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
) -> None:
    payload = sync_payload["response"]
    payload[field] = value

    with pytest.raises(SyncProtocolError, match=message):
        parse_sync_response(json.dumps(payload))


def test_duplicate_command_identities_are_rejected(
    sync_payload: dict[str, Any],
) -> None:
    command = sync_payload["response"]["commands"][0]
    sync_payload["response"]["commands"].append(dict(command))

    with pytest.raises(SyncProtocolError, match="duplicate command ident 92"):
        parse_sync_response(json.dumps(sync_payload["response"]))


def test_response_validates_embedded_configuration(
    sync_payload: dict[str, Any],
    config_payload: dict[str, Any],
) -> None:
    sync_payload["response"]["config"] = config_payload

    response = parse_sync_response(json.dumps(sync_payload["response"]))

    assert response.config is not None
    assert response.config.ident == config_payload["ident"]


def test_unknown_command_type_is_preserved_for_terminal_failure(
    sync_payload: dict[str, Any],
) -> None:
    command = sync_payload["response"]["commands"][0]
    command.update({"type": "future-command", "payload": {}})

    response = parse_sync_response(json.dumps(sync_payload["response"]))

    assert response.commands[0].type == "future-command"
    assert response.commands[0].payload == {}
