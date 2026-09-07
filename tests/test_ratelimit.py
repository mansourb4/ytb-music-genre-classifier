import pytest

from ytmgc.ratelimit import TokenBucket


class Clock:
    """Horloge virtuelle : les tests ne dorment jamais réellement."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def bucket(rate=60, burst=2):
    clock = Clock()
    return TokenBucket(rate, burst=burst, clock=lambda: clock.now, sleep=clock.sleep), clock


def test_burst_is_served_without_waiting():
    limiter, clock = bucket()
    assert [limiter.acquire() for _ in range(2)] == [0.0, 0.0]
    assert clock.slept == []


def test_requests_beyond_the_burst_are_throttled():
    limiter, clock = bucket()
    for _ in range(2):
        limiter.acquire()
    assert limiter.acquire() == pytest.approx(1.0)  # 60/min => 1 jeton par seconde


def test_tokens_refill_over_time():
    limiter, clock = bucket()
    limiter.acquire()
    limiter.acquire()
    clock.now += 5
    assert limiter.acquire() == 0.0


def test_invalid_rate_is_rejected():
    with pytest.raises(ValueError):
        TokenBucket(0)
