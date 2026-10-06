"""Tests for system-wide account-cycle supervision."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from sanex.accounting.clock import ActiveTimeClock
from sanex.accounting.cycle import AccountCycleResult, AppPolicyDecision, SessionObservation
from sanex.accounting.runtime import AccountRuntime
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.state import decode_runtime_state
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.service.supervisor import Supervisor


WALL_TIMESTAMP = 1_790_956_800


class FakeSessionSource:
    def __init__(self, sessions: tuple[LoginSession, ...]) -> None:
        self.snapshot = sessions
        self.calls = 0

    async def sessions(self) -> tuple[LoginSession, ...]:
        self.calls += 1
        return self.snapshot


class FakeWindowSource:
    def __init__(self, error_ident: str | None = None) -> None:
        self.error_ident = error_ident
        self.calls: list[str] = []

    async def reconcile(self, sessions: tuple[LoginSession, ...]) -> None:
        return None

    async def windows(self, session: LoginSession) -> AtspiWindowSnapshot:
        self.calls.append(session.ident)

        if session.ident == self.error_ident:
            raise RuntimeError("AT-SPI unavailable")

        return AtspiWindowSnapshot(
            windows=(AtspiWindow(
                bus=f":1.{session.ident}",
                path=f"/window/{session.ident}",
                pid=6000 + int(session.ident),
                title="Synthetic window",
                role=WindowRole.FRAME,
            ),),
        )


@dataclass(slots=True)
class FakeCycle:
    runtime: AccountRuntime
    collect: bool = True
    error: Exception | None = None
    calls: list[tuple[tuple[SessionObservation, ...], int, int, bool]] = field(
        default_factory=list
    )

    async def run(
        self,
        observations: tuple[SessionObservation, ...],
        timestamp: int,
        elapsed: int,
        existing: bool,
    ) -> AccountCycleResult:
        self.calls.append((observations, timestamp, elapsed, existing))

        if self.error is not None:
            raise self.error

        return AccountCycleResult(
            events=(),
            session_decisions=(),
            app_decision=AppPolicyDecision(),
            saved=False,
        )


def login_session(uid: int, ident: str) -> LoginSession:
    """Build a minimal graphical logind snapshot."""
    return LoginSession(
        ident=ident,
        uid=uid,
        login=f"user-{uid}",
        path=f"/session/{ident}",
        started=WALL_TIMESTAMP - 60,
        type=SessionType.WAYLAND,
        state=SessionState.ACTIVE,
        active=True,
        idle=False,
        locked=False,
    )


def clock_with_elapsed(seconds: int) -> ActiveTimeClock:
    """Build a deterministic active-time clock for one supervisor walk."""
    values = iter((0, seconds * 1_000_000_000))
    return ActiveTimeClock(now=lambda: next(values))


def fake_cycle(runtime_payload: dict[str, Any], uid: int, **changes: object) -> FakeCycle:
    """Build a lightweight cycle with a valid account runtime identity."""
    return FakeCycle(AccountRuntime(uid, decode_runtime_state(runtime_payload)), **changes)


@pytest.mark.asyncio
async def test_walk_uses_one_session_snapshot_and_routes_observations_by_uid(
    runtime_payload: dict[str, Any],
) -> None:
    sessions = (
        login_session(1001, "3"),
        login_session(1002, "4"),
        login_session(9999, "5"),
    )
    session_source = FakeSessionSource(sessions)
    window_source = FakeWindowSource()
    first = fake_cycle(runtime_payload, 1001)
    second = fake_cycle(runtime_payload, 1002, collect=False)
    supervisor = Supervisor(
        session_source=session_source,
        window_source=window_source,
        cycles={1001: first, 1002: second},
        clock=clock_with_elapsed(2),
        wall_clock=lambda: WALL_TIMESTAMP,
    )

    result = await supervisor.walk(existing=True)

    assert session_source.calls == 1
    assert window_source.calls == ["3"]
    assert result.timestamp == WALL_TIMESTAMP
    assert result.elapsed == 2
    assert [item.uid for item in result.accounts] == [1001, 1002]
    assert first.calls[0][0][0].session.ident == "3"
    assert first.calls[0][0][0].snapshot is not None
    assert first.calls[0][3]
    assert second.calls[0][0] == ()


@pytest.mark.asyncio
async def test_window_failure_is_forwarded_as_unavailable_observation(
    runtime_payload: dict[str, Any],
) -> None:
    session = login_session(1001, "3")
    cycle = fake_cycle(runtime_payload, 1001)
    supervisor = Supervisor(
        session_source=FakeSessionSource((session,)),
        window_source=FakeWindowSource(error_ident="3"),
        cycles={1001: cycle},
        clock=clock_with_elapsed(1),
        wall_clock=lambda: WALL_TIMESTAMP,
    )

    result = await supervisor.walk()

    assert result.accounts[0].error is None
    observation = cycle.calls[0][0][0]
    assert observation.session is session
    assert observation.snapshot is None


@pytest.mark.asyncio
async def test_account_failure_does_not_prevent_other_account_cycle(
    runtime_payload: dict[str, Any],
) -> None:
    failed = fake_cycle(
        runtime_payload,
        1001,
        collect=False,
        error=RuntimeError("broken account"),
    )
    healthy = fake_cycle(runtime_payload, 1002, collect=False)
    supervisor = Supervisor(
        session_source=FakeSessionSource(()),
        window_source=FakeWindowSource(),
        cycles={1001: failed, 1002: healthy},
        clock=clock_with_elapsed(1),
        wall_clock=lambda: WALL_TIMESTAMP,
    )

    result = await supervisor.walk()

    assert isinstance(result.accounts[0].error, RuntimeError)
    assert result.accounts[0].result is None
    assert result.accounts[1].error is None
    assert result.accounts[1].result is not None
    assert len(healthy.calls) == 1


@pytest.mark.asyncio
async def test_walk_accepts_elapsed_time_captured_at_sleep_transition(
    runtime_payload: dict[str, Any],
) -> None:
    readings = 0

    def monotonic_now() -> int:
        nonlocal readings
        readings += 1
        return 0

    clock = ActiveTimeClock(now=monotonic_now)
    cycle = fake_cycle(runtime_payload, 1001)
    supervisor = Supervisor(
        session_source=FakeSessionSource(()),
        window_source=FakeWindowSource(),
        cycles={1001: cycle},
        clock=clock,
        wall_clock=lambda: WALL_TIMESTAMP,
    )

    result = await supervisor.walk(elapsed=4)

    assert result.elapsed == 4
    assert cycle.calls[0][2] == 4
    assert readings == 1
