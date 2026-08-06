from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Optional

from backend.integrations.providers import get_provider

MASK = "••••••••"

# T01: a tenant with no configured inventory must get NOTHING, not the shared
# demo catalog. The old default enabled a "stub" source for every unconfigured
# tenant, which routes to StubPOSAdapter -> the process-wide SQLite
# products/orders tables. Those tables have no tenant_id column, so one tenant's
# agent would recite another's demo SKUs and write its customers' PII there.
# AdapterFactory falls back to StubPOSAdapter only for DEFAULT_TENANT_ID.
DEFAULT_INTEGRATIONS: Dict[str, Any] = {
    "inventory": {
        "enabled": True,
        "sources": [],
    },
    "crm": {"enabled": True, "provider": "internal", "config": {}},
    "calendar": {"enabled": True, "provider": "internal", "config": {}},
}


def _merge_inventory(incoming: Dict[str, Any], default: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(default)
    merged.update({k: incoming[k] for k in ("enabled",) if k in incoming})
    if "sources" in incoming:
        merged["sources"] = incoming["sources"]
    return merged


def _unwrap_nested_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    curr = config
    while isinstance(curr, dict) and list(curr.keys()) == ["config"]:
        curr = curr["config"]
    return curr if isinstance(curr, dict) else {}


def normalize_integrations(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = deepcopy(raw or {})
    result = deepcopy(DEFAULT_INTEGRATIONS)

    if raw.get("inventory"):
        result["inventory"] = _merge_inventory(raw["inventory"], result["inventory"])
    elif raw.get("pos"):
        legacy = raw.get("pos") or {}
        provider = (legacy.get("provider") or "stub").lower()
        result["inventory"]["sources"] = [
            {
                "id": "legacy_pos",
                "enabled": True,
                "provider": provider,
                "priority": 0,
                "label": provider,
                "config": {k: v for k, v in legacy.items() if k != "provider"},
            }
        ]

    if raw.get("crm"):
        crm = raw["crm"]
        if isinstance(crm, dict):
            cfg = crm.get("config")
            if isinstance(cfg, dict):
                cfg = _unwrap_nested_config(cfg)
            else:
                cfg = {k: v for k, v in crm.items() if k not in ("provider", "enabled", "config")}
            result["crm"] = {
                "enabled": crm.get("enabled", True),
                "provider": crm.get("provider", "internal"),
                "config": cfg,
            }

    if raw.get("calendar"):
        cal = raw["calendar"]
        if isinstance(cal, dict):
            cfg = cal.get("config")
            if isinstance(cfg, dict):
                cfg = _unwrap_nested_config(cfg)
            else:
                cfg = {k: v for k, v in cal.items() if k not in ("provider", "enabled", "config")}
            result["calendar"] = {
                "enabled": cal.get("enabled", True),
                "provider": cal.get("provider", "internal"),
                "config": cfg,
            }

    return result


_SECRET_KEY_PATTERN = re.compile(r"(password|token|secret|key|json)", re.IGNORECASE)


def mask_config(category: str, provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    provider = get_provider(category, provider_id)
    masked = deepcopy(config)
    if not provider:
        # S26: an unknown/legacy provider id used to return the raw config
        # unmasked. Fail closed — mask anything that looks like a secret key
        # rather than trusting the (missing) provider's declared field list.
        for key, value in list(masked.items()):
            if value and _SECRET_KEY_PATTERN.search(key):
                masked[key] = MASK
        return masked
    for key in provider.secret_fields:
        if key in masked and masked[key]:
            masked[key] = MASK
        enc_key = f"{key}_enc"
        if enc_key in masked and masked[enc_key]:
            masked[enc_key] = MASK
    return masked
