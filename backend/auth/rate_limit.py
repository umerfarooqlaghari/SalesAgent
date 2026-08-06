"""
Minimal in-memory sliding-window rate limiter for auth endpoints.

S13: login/register/forgot-password had no limiter, lockout or CAPTCHA at all.
This is intentionally dependency-free (no Redis requirement) — per-process
only, which is a real limitation behind multiple workers/replicas, but it is
a large improvement over "unlimited attempts" and needs no new infra to ship.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Dict, List

_MAX_BUCKETS = 10_000

# bucket_key -> list of monotonic timestamps within the current window
_HITS: "OrderedDict[str, List[float]]" = OrderedDict()


def check_rate_limit(bucket: str, identity: str, *, limit: int, window_seconds: float) -> bool:
    """
    Returns True if the call is allowed (and records it), False if the
    identity has exceeded `limit` calls within `window_seconds` for `bucket`.
    """
    key = f"{bucket}:{identity}"
    now = time.monotonic()
    hits = _HITS.get(key)
    if hits is None:
        hits = []
    else:
        _HITS.pop(key, None)

    cutoff = now - window_seconds
    hits = [t for t in hits if t > cutoff]

    allowed = len(hits) < limit
    if allowed:
        hits.append(now)

    _HITS[key] = hits
    _HITS.move_to_end(key)
    while len(_HITS) > _MAX_BUCKETS:
        _HITS.popitem(last=False)

    return allowed


def reset_rate_limits() -> None:
    """Test helper — clears all buckets."""
    _HITS.clear()
