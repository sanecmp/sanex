"""Tests for the public sanex configuration contract."""

import json
from typing import Any

import pytest
from pydantic import ValidationError
from sanelib.protocol import MatchType

from sanex.exceptions import ConfigError
from sanex.model.config import parse_config
from tests.sanex.assertions import assert_invalid_model


UNCHANGED = object()


def assert_invalid(value: object, message: str, path: str) -> None:
    """Validate fixture data through the production JSON parser."""
    assert_invalid_model(parse_config, ConfigError, json.dumps(value), message, path)


def test_valid_configuration_is_typed_and_frozen(config_payload: dict[str, Any]) -> None:
    config = parse_config(json.dumps(config_payload).encode())

    assert isinstance(config.accounts, tuple)
    limits = config.accounts[0].limits
    assert limits is not None
    assert limits.session_rules[0].app_rules[0].prc_name_match is MatchType.EXACT

    with pytest.raises(ValidationError):
        config.ident = 1


@pytest.mark.parametrize(
    ("field", "value", "message", "path"),
    [
        ("walk_interval", True, "Input should be a valid integer", "$.walk_interval"),
        ("timezone", "Not/A_Timezone", "unknown IANA timezone", "$.timezone"),
        ("discovery_interval", 301, "discovery_interval must not exceed sync_interval", "$"),
        ("walk_interval", 6, "walk_interval must not exceed save_interval", "$"),
    ],
)
def test_root_contract_rejects_invalid_values(
    config_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
    path: str,
) -> None:
    config_payload[field] = value
    assert_invalid(config_payload, message, path)


def test_unknown_and_missing_keys_are_rejected(config_payload: dict[str, Any]) -> None:
    config_payload["unexpected"] = True
    assert_invalid(config_payload, "Extra inputs are not permitted", "$.unexpected")

    config_payload.pop("unexpected")
    config_payload.pop("accounts")
    assert_invalid(config_payload, "Field required", "$.accounts")


@pytest.mark.parametrize(
    ("collect", "limits", "message"),
    [
        (False, UNCHANGED, "apply=true requires collect=true"),
        (True, None, "apply=true requires limits"),
    ],
)
def test_account_enforcement_dependencies(
    config_payload: dict[str, Any],
    collect: bool,
    limits: object,
    message: str,
) -> None:
    account = config_payload["accounts"][0]
    account["collect"] = collect

    if limits is not UNCHANGED:
        account["limits"] = limits

    assert_invalid(config_payload, message, "$.accounts[0]")


def test_range_must_reference_an_existing_rule(config_payload: dict[str, Any]) -> None:
    limits = config_payload["accounts"][0]["limits"]
    limits["ranges"][0]["session_rule_ident"] = 999

    assert_invalid(config_payload, "references unknown SessionRule 999", "$.accounts[0].limits")


def test_applied_ranges_must_not_overlap(config_payload: dict[str, Any]) -> None:
    limits = config_payload["accounts"][0]["limits"]
    limits["ranges"].append(
        {
            "ident": 502,
            "apply": False,
            "weekday": 4,
            "since": 1380,
            "till": 1440,
            "session_rule_ident": 601,
        }
    )
    parse_config(json.dumps(config_payload))

    limits["ranges"][1]["apply"] = True
    assert_invalid(config_payload, "Range 502 overlaps Range 501", "$.accounts[0].limits")


def test_identifiers_and_account_uids_must_be_unique(config_payload: dict[str, Any]) -> None:
    duplicate_account = dict(config_payload["accounts"][0])
    config_payload["accounts"].append(duplicate_account)
    assert_invalid(config_payload, "duplicate Account uid 1001", "$")

    config_payload["accounts"].pop()
    limits = config_payload["accounts"][0]["limits"]
    limits["ranges"].append(dict(limits["ranges"][0]))
    assert_invalid(config_payload, "duplicate Range ident 501", "$.accounts[0].limits")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_launches": None, "max_time": None}, "at least one limit is required"),
        (
            {
                "prc_name": None,
                "prc_name_match": None,
                "exe": None,
                "exe_match": None,
                "wnd_title": None,
                "wnd_title_match": None,
            },
            "at least one recognition condition is required",
        ),
        ({"prc_name_match": None}, "prc_name_match is required when prc_name is set"),
        (
            {"prc_name": "[", "prc_name_match": "regex"},
            "prc_name is not a valid regular expression",
        ),
    ],
)
def test_application_rule_contract(
    config_payload: dict[str, Any],
    changes: dict[str, object],
    message: str,
) -> None:
    rule = config_payload["accounts"][0]["limits"]["session_rules"][0]["app_rules"][0]
    rule.update(changes)
    assert_invalid(config_payload, message, "$.accounts[0].limits.session_rules[0].app_rules[0]")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("{\"ident\": 1, \"ident\": 2}", "duplicate object key 'ident'"),
        ("{\"ident\":", "invalid JSON"),
        ("{\"ident\": NaN}", "invalid JSON constant NaN"),
    ],
)
def test_invalid_json_is_reported_as_config_error(data: str, message: str) -> None:
    assert_invalid_model(parse_config, ConfigError, data, message, "$")
