from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

from backend.integrations.providers import get_provider

MASK = "••••••••"

DEFAULT_INTEGRATIONS: Dict[str, Any] = {
    "inventory": {
        "enabled": True,
        "sources": [
            {
                "id": "default_stub",
                "enabled": True,
                "provider": "stub",
                "priority": 0,
                "config": {"read_only": True},
            }
        ],
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
            result["crm"] = {
                "enabled": crm.get("enabled", True),
                "provider": crm.get("provider", "internal"),
                "config": {k: v for k, v in crm.items() if k not in ("provider", "enabled")},
            }

    if raw.get("calendar"):
        cal = raw["calendar"]
        if isinstance(cal, dict):
            result["calendar"] = {
                "enabled": cal.get("enabled", True),
                "provider": cal.get("provider", "internal"),
                "config": {k: v for k, v in cal.items() if k not in ("provider", "enabled")},
            }

    return result


def mask_config(category: str, provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    provider = get_provider(category, provider_id)
    if not provider:
        return config
    masked = deepcopy(config)
    for key in provider.secret_fields:
        if key in masked and masked[key]:
            masked[key] = MASK
        enc_key = f"{key}_enc"
        if enc_key in masked and masked[enc_key]:
            masked[enc_key] = MASK
    return masked
