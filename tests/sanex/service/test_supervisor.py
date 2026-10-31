"""Tests for system-wide account-cycle supervision."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from sanex.accounting.clock import ActiveTimeClock
from sanex.accounting.cycle import AccountCycleResult, AppPolicyDecision, SessionObservation
from sanex.accounting.runtime import AccountRuntime
from sanex.exceptions import AccountingError
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.state import decode_runtime_state
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.service.supervisor import Supervisor


WALL_TIMESTAMP = 1_790_956_800


class FakeSessionSource:
    def __init__(self, sessions: tuple[LoginSession, ...]) -> None:
        self.snapshot = sessions
        self.calls = 0

    async def read_sessions(self) -> tuple[LoginSession, ...]:
        self.calls += 1
        return self.snapshot


class FakeWindowSource:
    def __init__(self, error_ident: str | None = None) -> None:
        self.error_ident = error_ident
        self.calls: list[str] = []
        self.reconciled: list[tuple[LoginSession, ...]] = []

    async def reconcile(self, sessions: tuple[LoginSession, ...]) -> None:
        self.reconciled.append(sessions)

    async def read_windows(self, session: LoginSession) -> AtspiWindowSnapshot:
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
    assert result.sessions == sessions
    assert [item.uid for item in result.accounts] == [1001, 1002]
    assert first.calls[0][0][0].session.ident == "3"
    assert first.calls[0][0][0].snapshot is not None
    assert first.calls[0][3]
    assert second.calls[0][0] == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "observed_idents"),
    [
        (SessionState.OPENING, ("4",)),
        (SessionState.ONLINE, ("3", "4")),
        (SessionState.ACTIVE, ("3", "4")),
        (SessionState.CLOSING, ("4",)),
    ],
)
async def test_walk_accounts_all_sessions_but_observes_only_ready_ones(
    runtime_payload: dict[str, Any], state: SessionState, observed_idents: tuple[str, ...],
) -> None:
    sessions = (
        login_session(1001, "3").model_copy(update={"state": state, "active": state is SessionState.ACTIVE}),
        login_session(1001, "4").model_copy(update={"state": SessionState.ONLINE, "active": False}),
    )
    cycle = fake_cycle(runtime_payload, 1001)
    windows = FakeWindowSource()
    supervisor = Supervisor(
        session_source=FakeSessionSource(sessions),
        window_source=windows,
        cycles={1001: cycle},
        clock=clock_with_elapsed(1),
        wall_clock=lambda: WALL_TIMESTAMP,
    )

    result = await supervisor.walk()

    assert result.accounts[0].error is None
    assert result.sessions == sessions
    assert tuple(windows.calls) == observed_idents
    assert tuple(session.ident for session in windows.reconciled[0]) == observed_idents
    observations = cycle.calls[0][0]
    assert tuple(observation.session for observation in observations) == sessions
    assert all(
        (observation.snapshot is not None) == (observation.session.ident in observed_idents)
        for observation in observations
    )


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
        wall_clock=lambda: WALL_TIMESTAMP + 86_400,
    )

    result = await supervisor.walk(elapsed=4, timestamp=WALL_TIMESTAMP)

    assert result.elapsed == 4
    assert result.timestamp == WALL_TIMESTAMP
    assert cycle.calls[0][1] == WALL_TIMESTAMP
    assert cycle.calls[0][2] == 4
    assert readings == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [-1, True, 1.5])
async def test_walk_rejects_an_invalid_captured_timestamp(timestamp: object) -> None:
    sessions = FakeSessionSource(())
    supervisor = Supervisor(
        session_source=sessions,
        window_source=FakeWindowSource(),
        cycles={},
        clock=ActiveTimeClock(now=lambda: 0),
        wall_clock=lambda: WALL_TIMESTAMP,
    )

    with pytest.raises(AccountingError, match="supervisor timestamp is invalid"):
        await supervisor.walk(elapsed=4, timestamp=timestamp)

    assert sessions.calls == 0
