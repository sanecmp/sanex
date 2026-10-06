"""Tests for the public sanex runtime-state contract."""

from datetime import date
from functools import partial
from typing import Any

import pytest

from sanex.exceptions import RuntimeStateError
from sanex.model.state import decode_runtime_state
from tests.sanex.assertions import assert_invalid_model


assert_invalid = partial(assert_invalid_model, decode_runtime_state, RuntimeStateError)


def test_valid_state_is_typed_and_mutable(runtime_payload: dict[str, Any]) -> None:
    state = decode_runtime_state(runtime_payload)

    assert state.occurrences[0].date == date(2026, 10, 1)
    assert state.prc_runs[0].wnds == [42]
    assert state.prc_runs[0].group_ident == "6443:5753094"
    state.event_seq += 1
    assert state.event_seq == 142


def test_process_run_requires_group_identity(runtime_payload: dict[str, Any]) -> None:
    del runtime_payload["prc_runs"][0]["group_ident"]

    assert_invalid(runtime_payload, "Field required", "$.prc_runs[0].group_ident")


@pytest.mark.parametrize(
    ("group_ident", "message"),
    [
        (None, "Input should be a valid string"),
        ("", "String should have at least 1 character"),
    ],
)
def test_process_group_identity_must_be_nonempty(
    runtime_payload: dict[str, Any],
    group_ident: object,
    message: str,
) -> None:
    runtime_payload["prc_runs"][0]["group_ident"] = group_ident

    assert_invalid(runtime_payload, message, "$.prc_runs[0].group_ident")


@pytest.mark.parametrize(
    ("reported", "run_ident"),
    [
        (False, 121),
        (True, None),
    ],
)
def test_process_reporting_requires_an_event_identity(
    runtime_payload: dict[str, Any],
    reported: bool,
    run_ident: int | None,
) -> None:
    run = runtime_payload["prc_runs"][0]
    run["reported"] = reported
    run["run_ident"] = run_ident

    assert_invalid(
        runtime_payload,
        "reported must be true exactly when run_ident is set",
        "$.prc_runs[0]",
    )


def test_active_application_windows_must_be_counted(runtime_payload: dict[str, Any]) -> None:
    runtime_payload["occurrences"][0]["apps"][0]["active_wnds"] = [43]

    assert_invalid(
        runtime_payload,
        "active_wnds contains uncounted window 43",
        "$.occurrences[0].apps[0]",
    )


@pytest.mark.parametrize(
    ("field", "value", "message", "path"),
    [
        ("event_seq", 120, "event_seq is lower than an active run ident", "$"),
        ("wnd_seq", 41, "wnd_seq is lower than an active window ident", "$"),
        ("break_till", -1, "Input should be greater than or equal to 0", "$.break_till"),
        ("unexpected", True, "Extra inputs are not permitted", "$.unexpected"),
    ],
)
def test_root_contract_rejects_invalid_values(
    runtime_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
    path: str,
) -> None:
    runtime_payload[field] = value

    assert_invalid(runtime_payload, message, path)


def test_event_run_identifiers_are_unique_across_event_types(
    runtime_payload: dict[str, Any],
) -> None:
    runtime_payload["prc_runs"][0]["run_ident"] = 120

    assert_invalid(runtime_payload, "duplicate event run ident 120", "$")


def test_range_occurrences_are_unique(runtime_payload: dict[str, Any]) -> None:
    runtime_payload["occurrences"].append(runtime_payload["occurrences"][0].copy())

    assert_invalid(
        runtime_payload,
        "duplicate range occurrence (501, datetime.date(2026, 10, 1))",
        "$",
    )


def test_session_run_break_snapshot_requires_termination(
    runtime_payload: dict[str, Any],
) -> None:
    runtime_payload["sess_runs"][0]["break_duration"] = 120

    assert_invalid(
        runtime_payload,
        "break_duration must be zero without terminate_requested",
        "$.sess_runs[0]",
    )
