"""Retry failed calls with exponential backoff, not with blind loops.

Practice: "Retry failed tool calls with backoff, not blind loops."

A blind loop ("try again until it works") makes an outage worse: every client
hits the struggling service again at once. Backoff waits longer after each
failure (0.5 s, 1 s, 2 s ...), adds random jitter so that clients spread out,
and stops after a fixed number of attempts. Only transient errors are retried:
a timeout or HTTP 503 can heal; a 404 or a schema error will never heal.

The circuit breaker is the next layer. After N failures in a row it stops
calling the service for a cool-down period, and the caller uses a fallback.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """An error that can heal: timeout, connection reset, HTTP 429/5xx."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """An error that will not heal by itself: bad input, 404, schema error."""


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    factor: float = 2.0
    max_delay: float = 8.0
    jitter: bool = True

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before attempt number `attempt` + 1 (attempt starts at 1)."""
        cap = min(self.max_delay, self.base_delay * self.factor ** (attempt - 1))
        if not self.jitter:
            return cap
        return (rng or random).uniform(0, cap)  # "full jitter"


@dataclass
class Attempt:
    number: int
    error: str
    waited: float


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    on_retry: Callable[[Attempt], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == policy.max_attempts:
                raise
            wait = policy.delay(attempt)
            hint = getattr(exc, "retry_after", None)
            if hint:
                wait = max(wait, min(float(hint), policy.max_delay))  # respect Retry-After
            if on_retry:
                on_retry(Attempt(attempt, f"{type(exc).__name__}: {exc}", wait))
            sleep(wait)
    raise AssertionError("unreachable")


class CircuitOpen(Exception):
    """The breaker is open: do not call the service now, use the fallback."""


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None
    clock: Callable[[], float] = field(default=time.monotonic)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self.clock() - self._opened_at >= self.cooldown_s:
            return "half-open"  # let one request through to test the service
        return "open"

    def call(self, fn: Callable[[], T]) -> T:
        if self.state == "open":
            raise CircuitOpen(f"{self.name} circuit is open")
        try:
            result = fn()
        except (TransientError, CircuitOpen):
            self._failures += 1
            if self._failures >= self.failure_threshold or self.state == "half-open":
                self._opened_at = self.clock()
            raise
        self._failures = 0
        self._opened_at = None
        return result

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None
