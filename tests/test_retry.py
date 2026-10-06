import pytest

from refund_desk.retry import (CircuitBreaker, CircuitOpen, PermanentError, RetryPolicy, TransientError,
                               call_with_retry)


def test_backoff_grows_and_is_capped():
    p = RetryPolicy(base_delay=0.5, factor=2, max_delay=4, jitter=False)
    assert [p.delay(n) for n in range(1, 6)] == [0.5, 1, 2, 4, 4]


def test_retries_transient_errors_then_succeeds():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("503")
        return "ok"
    assert call_with_retry(flaky, RetryPolicy(max_attempts=4), sleep=waits.append) == "ok"
    assert len(calls) == 3 and len(waits) == 2


def test_does_not_retry_permanent_errors():
    calls = []

    def bad():
        calls.append(1)
        raise PermanentError("404")
    with pytest.raises(PermanentError):
        call_with_retry(bad, RetryPolicy(max_attempts=4), sleep=lambda s: None)
    assert len(calls) == 1


def test_gives_up_after_max_attempts():
    with pytest.raises(TransientError):
        call_with_retry(lambda: (_ for _ in ()).throw(TransientError("down")), RetryPolicy(max_attempts=3),
                        sleep=lambda s: None)


def test_circuit_breaker_opens_then_half_opens():
    now = [0.0]
    cb = CircuitBreaker("api", failure_threshold=2, cooldown_s=30, clock=lambda: now[0])

    def fail():
        raise TransientError("down")
    for _ in range(2):
        with pytest.raises(TransientError):
            cb.call(fail)
    assert cb.state == "open"
    with pytest.raises(CircuitOpen):
        cb.call(lambda: "never called")
    now[0] = 31
    assert cb.state == "half-open"
    assert cb.call(lambda: "ok") == "ok" and cb.state == "closed"
