"""Tests for strict window-agent protocol messages."""

import pytest

from sanex.exceptions import WindowAgentError
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.model.window_agent import (
    CloseRequest,
    WindowsResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)


def sample_window() -> AtspiWindow:
    return AtspiWindow(
        bus=":1.188",
        path="/org/a11y/atspi/accessible/124",
        pid=6443,
        title="Synthetic window",
        role=WindowRole.FRAME,
    )


def test_protocol_messages_round_trip() -> None:
    request = CloseRequest(
        ident=7,
        op="close",
        sess_ident="3",
        window=sample_window(),
    )
    response = WindowsResponse(
        ident=7,
        op="windows",
        snapshot=AtspiWindowSnapshot(windows=(sample_window(),)),
    )

    assert decode_request(encode_request(request)) == request
    assert decode_response(encode_response(response)) == response


def test_unknown_fields_are_rejected() -> None:
    data = b"{\"ident\":1,\"op\":\"windows\",\"sess_ident\":\"3\",\"uid\":1001}"

    with pytest.raises(WindowAgentError, match="decode request"):
        decode_request(data)
