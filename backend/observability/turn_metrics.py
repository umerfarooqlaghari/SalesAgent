"""
Per-turn outcome telemetry.

WHY
---
The reported symptom is "at night it answers everything, in the day it gets
dumb". That is currently un-diagnosable, because every way a turn can degrade
produces a near-identical apology to the caller and nothing durable anywhere:

  * Gemini refused with RESOURCE_EXHAUSTED / 429  -> "brief system delay"
  * the 7s turn deadline elapsed                  -> "didn't quite catch that"
  * a tool raised                                 -> "hit a small snag"
  * the catalog cache was cold so the model
    answered from the prompt alone                -> a fluent, generic,
                                                     factually thin answer
                                                     (this one looks like the
                                                     model "getting dumb")

Those have four different fixes and one indistinguishable symptom. This module
records what actually happened, with a timestamp, so the question is settled by
data instead of argument.

DESIGN
------
* Fire-and-forget: `record_turn(...)` never raises and never blocks the turn.
  A telemetry failure must not become a customer-facing failure.
* Capped collection (16 MB) so it cannot grow without bound, plus a TTL index,
  so this needs no operational babysitting.
* No message content, no PII — outcome code, durations, and coarse flags only.
  Safe to read in a shared channel.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

COLLECTION = "turn_metrics"
_CAP_BYTES = 16 * 1024 * 1024
_RETENTION_DAYS = 14

# Stable outcome codes. Add to this list, never renumber or reword — the report
# groups on them and old rows must stay comparable.
OK = "ok"
OK_FASTPATH = "ok_fastpath"
OK_CACHED = "ok_cached"
QUOTA_EXHAUSTED = "quota_exhausted"        # Gemini 429 / RESOURCE_EXHAUSTED
TURN_DEADLINE = "turn_deadline"            # our own 7s budget elapsed
RECURSION_LIMIT = "recursion_limit"
TOOL_ERROR = "tool_error"
CONFIG_ERROR = "config_error"              # e.g. undecryptable integration secret
BILLING_BLOCKED = "billing_blocked"
UNKNOWN_ERROR = "unknown_error"

_ensured = False


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a stable outcome code."""
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "resource_exhausted" in lowered or "429" in lowered or "quota" in lowered:
        return QUOTA_EXHAUSTED
    if "rate limit" in lowered or "ratelimit" in lowered:
        return QUOTA_EXHAUSTED
    if type(exc).__name__ == "GraphRecursionError":
        return RECURSION_LIMIT
    if isinstance(exc, asyncio.TimeoutError):
        return TURN_DEADLINE
    if type(exc).__name__ == "SecretDecryptionError":
        return CONFIG_ERROR
    return UNKNOWN_ERROR


async def _ensure_collection(db) -> None:
    global _ensured
    if _ensured:
        return
    _ensured = True
    try:
        names = await db.list_collection_names()
        if COLLECTION not in names:
            await db.create_collection(COLLECTION, capped=True, size=_CAP_BYTES)
    except Exception:
        logger.debug("turn_metrics: could not create capped collection", exc_info=True)
    try:
        # A capped collection cannot use a TTL index, so only add one if the
        # collection turned out not to be capped (e.g. it pre-existed).
        stats_ok = False
        try:
            opts = await db[COLLECTION].options()
            stats_ok = bool(opts.get("capped"))
        except Exception:
            stats_ok = False
        if not stats_ok:
            await db[COLLECTION].create_index(
                "created_at", expireAfterSeconds=_RETENTION_DAYS * 86400,
                name="turn_metrics_ttl",
            )
        await db[COLLECTION].create_index([("tenant_id", 1), ("ts", -1)],
                                          name="turn_metrics_tenant_ts")
    except Exception:
        logger.debug("turn_metrics: index setup skipped", exc_info=True)


async def record_turn(
    *,
    tenant_id: str,
    channel: str,                      # "voice" | "chat" | "query"
    outcome: str,
    duration_ms: float,
    model: str = "",
    catalog_warm: Optional[bool] = None,
    tool_calls: int = 0,
    detail: str = "",
) -> None:
    """
    Persist one turn outcome. Never raises.

    `detail` is truncated hard — it is for an exception class or a short reason,
    never for message content.
    """
    try:
        from backend.database import get_db

        db = get_db()
        await _ensure_collection(db)
        now = datetime.now(timezone.utc)
        await db[COLLECTION].insert_one(
            {
                "ts": now.isoformat(),
                "created_at": now,
                # Local wall-clock hour is what the "night vs day" question is
                # actually about, so store the UTC hour and let the report shift
                # it — never guess a timezone here.
                "utc_hour": now.hour,
                "tenant_id": tenant_id,
                "channel": channel,
                "outcome": outcome,
                "duration_ms": round(float(duration_ms), 1),
                "model": model,
                "catalog_warm": catalog_warm,
                "tool_calls": int(tool_calls),
                "detail": (detail or "")[:200],
            }
        )
    except Exception:
        # Telemetry must never break a turn.
        logger.debug("turn_metrics: record failed", exc_info=True)


def record_turn_bg(**kwargs) -> None:
    """Schedule record_turn without awaiting it, for hot paths."""
    try:
        asyncio.get_running_loop().create_task(record_turn(**kwargs))
    except RuntimeError:
        pass
    except Exception:
        logger.debug("turn_metrics: scheduling failed", exc_info=True)
