"""
Per-tenant Adapter-Hub keys, derived rather than stored.

S10: authentication and tenancy were decoupled. One shared master key proved
"some caller holds the key", and the tenant was whatever that caller wrote in
`X-Tenant-ID`. So any component with the master key — or anyone who ever saw it,
and it shipped in source as `adapter-hub-super-secret-key` — could read and
write every tenant's knowledge base by editing one header.

The obvious fix is a per-tenant key table, but the hub has no tenant store and
adding one means a migration, a sync path and a new failure mode. Deriving the
key gets the same security property with no storage at all:

    tenant_key = "ahk_" + urlsafe_b64( HMAC-SHA256(master_key, "adapter-hub-tenant-v1:" + tenant_id) )

The caller sends the derived key plus the tenant id it claims. The hub recomputes
the expected key for the CLAIMED tenant and compares in constant time. Holding
tenant A's key tells you nothing about tenant B's, because inverting HMAC without
the master key is infeasible. The header is no longer trusted — it is verified.

Properties:
  * no storage, no migration, no sync
  * rotating the master key rotates every tenant key at once
  * the master key itself stops being a usable credential on tenant routes
  * a leaked tenant key is scoped to that one tenant
"""
from __future__ import annotations

import base64
import hashlib
import hmac

PREFIX = "ahk_"
_DOMAIN = b"adapter-hub-tenant-v1:"


def derive_tenant_key(master_key: str, tenant_id: str) -> str:
    """The key a caller must present to act as `tenant_id`."""
    if not master_key:
        raise ValueError("A master key is required to derive tenant keys.")
    if not tenant_id:
        raise ValueError("A tenant id is required to derive a tenant key.")
    digest = hmac.new(
        master_key.encode("utf-8"),
        _DOMAIN + tenant_id.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return PREFIX + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_tenant_key(master_key: str, tenant_id: str, presented: str) -> bool:
    """
    Constant-time check that `presented` is the key for `tenant_id`.

    Never raises on malformed input — a bad key is a 401, not a 500. The old
    `hmac.compare_digest` call took `str` arguments and raised TypeError on a
    non-ASCII header, turning any request into a 500.
    """
    if not presented or not tenant_id or not master_key:
        return False
    try:
        expected = derive_tenant_key(master_key, tenant_id)
    except ValueError:
        return False
    return hmac.compare_digest(
        presented.encode("utf-8", "replace"), expected.encode("utf-8")
    )
