from __future__ import annotations

from ssc.weather.circuit import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_closed_allows_requests_and_ignores_a_lone_failure() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=60.0, clock=clock)

    assert breaker.allow_request()
    breaker.record_failure()
    assert breaker.state == CircuitState.closed
    assert breaker.allow_request()


def test_opens_after_threshold_failures_and_refuses_requests() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=60.0, clock=clock)

    for _ in range(3):
        breaker.allow_request()
        breaker.record_failure()

    assert breaker.state == CircuitState.open
    assert not breaker.allow_request()


def test_half_open_probe_after_reset_timeout_then_close_on_success() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=10.0, clock=clock)

    breaker.allow_request()
    breaker.record_failure()
    assert breaker.state == CircuitState.open
    assert not breaker.allow_request()

    clock.advance(10.0)
    assert breaker.allow_request()  # the probe
    assert breaker.state == CircuitState.half_open
    assert not breaker.allow_request()  # only one probe in flight at a time

    breaker.record_success()
    assert breaker.state == CircuitState.closed
    assert breaker.allow_request()


def test_half_open_probe_failure_reopens_and_restarts_timeout() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=10.0, clock=clock)

    breaker.allow_request()
    breaker.record_failure()
    clock.advance(10.0)
    breaker.allow_request()
    breaker.record_failure()

    assert breaker.state == CircuitState.open
    assert not breaker.allow_request()
    clock.advance(9.9)
    assert not breaker.allow_request()
    clock.advance(0.1)
    assert breaker.allow_request()


def test_force_open_honours_retry_after_even_if_longer_than_default() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=10.0, clock=clock)

    breaker.force_open(for_s=120.0)
    assert breaker.state == CircuitState.open
    clock.advance(10.0)
    assert not breaker.allow_request()  # default reset would have let this through
    clock.advance(110.0)
    assert breaker.allow_request()
