"""Persistent backoff for deferred execution requests (lock held / project busy / lease held)."""
import random

from app.validation import policies as P


def defer_delay(defer_count: int, jitter: float | None = None) -> float:
    """Delay for the NEXT defer. `defer_count` is the number of defers already recorded (before this one), so the
    first delay is BASE, then 2*BASE, 4*BASE ... up to MAX. `jitter` is a multiplier; None draws one."""
    base = min(P.DEFER_MAX_SECONDS, P.DEFER_BASE_SECONDS * (2 ** max(0, defer_count)))
    factor = random.uniform(*P.DEFER_JITTER) if jitter is None else jitter
    return base * factor
