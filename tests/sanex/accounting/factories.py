"""Shared factories for accounting tests."""

from typing import Any

from sanelib.protocol import AppRule, decode_config

from sanex.model.state import RangeOccurrence, decode_runtime_state


def configured_app_rule(config_payload: dict[str, Any], **changes: object) -> AppRule:
    """Build a validated application rule from the shared fixture."""
    config = decode_config(config_payload)
    limits = config.accounts[0].limits
    assert limits is not None
    payload = limits.session_rules[0].app_rules[0].model_dump()
    payload.update(changes)
    return AppRule.model_validate(payload)


def runtime_occurrence(runtime_payload: dict[str, Any]) -> RangeOccurrence:
    """Load the range occurrence from the shared runtime fixture."""
    return decode_runtime_state(runtime_payload).occurrences[0]
