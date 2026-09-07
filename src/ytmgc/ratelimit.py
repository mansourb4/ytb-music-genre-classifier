"""Limiteur de débit à seau de jetons, utilisé pour l'API Discogs.

L'horloge et la temporisation sont injectables : les tests n'attendent jamais.
"""

from __future__ import annotations

import time
from typing import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: int,
        *,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute doit être strictement positif")
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(burst if burst is not None else max(1, rate_per_minute // 10))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)

    def acquire(self, tokens: float = 1.0) -> float:
        """Consomme un jeton, en attendant si nécessaire. Renvoie l'attente subie."""
        if tokens > self._capacity:
            raise ValueError("Demande supérieure à la capacité du seau")
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        wait = (tokens - self._tokens) / self._rate_per_second
        self._sleep(wait)
        self._refill()
        self._tokens = max(0.0, self._tokens - tokens)
        return wait
