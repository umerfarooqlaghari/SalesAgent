"""
Tenant-scoped LangGraph checkpoint keys (audit T09/T10).

`MongoDBSaver` keys checkpoints on `(thread_id, checkpoint_ns, checkpoint_id)`
and reads `checkpoint_ns` from `config["configurable"]`. LangGraph's Pregel
runtime overwrites `checkpoint_ns` with "" at the top level, so it cannot be
used for tenancy — the only field we control is `thread_id`.

Left unscoped, `thread_id` is entirely client-supplied (a path parameter on
`/ws/chat/{thread_id}`, a body field on `/api/query`, `console_thread_id` on the
embed session) and predictable, so tenant A could resume tenant B's conversation
state: full message history, extracted lead PII and prior tool results.

So the checkpoint key is namespaced, while the *logical* thread id — the one
used for `conversations`, `leads`, `appointments` and `orders` — stays clean.
Tools must therefore read `logical_thread_id(config)` rather than
`config["configurable"]["thread_id"]`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

SEP = "::"


def scoped_thread_id(tenant_id: str, thread_id: str) -> str:
    """Checkpoint key for this tenant's copy of `thread_id`."""
    tid = (tenant_id or "").strip() or "unknown_tenant"
    raw = (thread_id or "").strip() or "default_thread"
    if raw.startswith(f"{tid}{SEP}"):
        return raw          # already scoped — never double-prefix
    return f"{tid}{SEP}{raw}"


def unscope_thread_id(tenant_id: str, value: str) -> str:
    """Inverse of `scoped_thread_id`; returns `value` unchanged if not scoped."""
    prefix = f"{(tenant_id or '').strip()}{SEP}"
    if tenant_id and value and value.startswith(prefix):
        return value[len(prefix):]
    return value


def graph_config(
    tenant_id: str,
    thread_id: str,
    *,
    recursion_limit: Optional[int] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a RunnableConfig whose checkpoints cannot collide across tenants."""
    configurable: Dict[str, Any] = {
        "thread_id": scoped_thread_id(tenant_id, thread_id),
        "logical_thread_id": thread_id,
        "tenant_id": tenant_id,
    }
    configurable.update(extra)
    config: Dict[str, Any] = {"configurable": configurable}
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    return config


def logical_thread_id(config: Any) -> str:
    """
    The un-namespaced thread id, for tools writing tenant-scoped records.

    Falls back to stripping the prefix so an old in-flight config (or a caller
    that builds its own) still resolves correctly.
    """
    cfg = (config or {}).get("configurable", {}) or {}
    explicit = cfg.get("logical_thread_id")
    if explicit:
        return str(explicit)
    raw = cfg.get("thread_id") or "default_thread"
    return unscope_thread_id(cfg.get("tenant_id") or "", str(raw))
