from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_db
from backend.integrations.normalize import DEFAULT_INTEGRATIONS, MASK, mask_config, normalize_integrations
from backend.integrations.providers import get_provider
from backend.tenant.context import TenantContext
from backend.tenant.registry import get_tenant_by_id
from backend.tenant.secrets import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

SECRET_SUFFIX = "_enc"
SQL_PROVIDERS = {"postgres", "sqlserver", "mysql"}


def _integrations_use_sql(integrations: Dict[str, Any]) -> bool:
    crm = integrations.get("crm") or {}
    if crm.get("enabled", True) and (crm.get("provider") or "").lower() in SQL_PROVIDERS:
        return True
    inv = integrations.get("inventory") or {}
    for src in inv.get("sources") or []:
        if src.get("enabled", True) and (src.get("provider") or "").lower() in SQL_PROVIDERS:
            return True
    return False


def _disable_demo_stub_sources(integrations: Dict[str, Any]) -> None:
    """Turn off the built-in Alpha demo catalog when a real SQL source is configured."""
    if not _integrations_use_sql(integrations):
        return
    inv = integrations.get("inventory") or {}
    for src in inv.get("sources") or []:
        provider = (src.get("provider") or "").lower()
        if provider in ("stub", "sqlite"):
            src["enabled"] = False


async def _maybe_refresh_tenant_prompt(db, tenant_id: str, org_name: str, current_prompt: str) -> None:
    from backend.agent.prompts import build_tenant_system_prompt, is_alpha_default_prompt

    if not is_alpha_default_prompt(current_prompt):
        return
    settings = (await db.tenants.find_one({"tenant_id": tenant_id}) or {}).get("settings") or {}
    description = settings.get("company_description") or ""
    await db.tenants.update_one(
        {"tenant_id": tenant_id},
        {
            "$set": {
                "settings.system_prompt": build_tenant_system_prompt(org_name, description),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    logger.info("Refreshed Alpha demo prompt for tenant %s (%s)", tenant_id, org_name)


class IntegrationService:
    @staticmethod
    def mask_config(category: str, provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        return mask_config(category, provider_id, config)

    @staticmethod
    def prepare_config_for_storage(
        category: str,
        provider_id: str,
        config: Dict[str, Any],
        existing: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        provider = get_provider(category, provider_id)
        if not provider:
            raise ValueError(f"Unknown provider '{provider_id}' for category '{category}'")

        existing = existing or {}
        stored: Dict[str, Any] = {}

        for field in provider.fields:
            key = field.key
            if key not in config:
                if key in existing:
                    stored[key] = existing[key]
                continue
            value = config[key]
            if key in provider.secret_fields:
                if value in (None, "", MASK) or (isinstance(value, str) and value == MASK):
                    enc_key = f"{key}{SECRET_SUFFIX}"
                    if enc_key in existing:
                        stored[enc_key] = existing[enc_key]
                    elif key in existing:
                        stored[key] = existing[key]
                    continue
                try:
                    stored[f"{key}{SECRET_SUFFIX}"] = encrypt_secret(str(value))
                except RuntimeError:
                    stored[key] = str(value)
            elif field.field_type == "json" and isinstance(value, str):
                try:
                    stored[key] = json.loads(value) if value.strip() else {}
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON for {key}: {e}") from e
            elif field.field_type == "boolean":
                stored[key] = bool(value)
            elif field.field_type == "number" and value not in (None, ""):
                stored[key] = int(value)
            else:
                stored[key] = value

        return stored

    @staticmethod
    def resolve_secrets(category: str, provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Return config with decrypted secrets inlined for adapter use."""
        provider = get_provider(category, provider_id)
        if not provider:
            return config
        resolved = deepcopy(config)
        for key in provider.secret_fields:
            enc_key = f"{key}{SECRET_SUFFIX}"
            if enc_key in resolved:
                try:
                    resolved[key] = decrypt_secret(resolved[enc_key])
                except Exception as e:
                    logger.error("Failed to decrypt %s for %s/%s: %s", key, category, provider_id, e)
                    resolved[key] = ""
                del resolved[enc_key]
            elif key not in resolved and key in config:
                resolved[key] = config.get(key, "")
        return resolved

    @staticmethod
    async def get_tenant_integrations(tenant_id: str) -> Dict[str, Any]:
        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            raise ValueError("Tenant not found")
        raw = ctx.integrations_raw if hasattr(ctx, "integrations_raw") else None
        return normalize_integrations(raw)

    @staticmethod
    async def get_admin_view(tenant_id: str) -> Dict[str, Any]:
        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            raise ValueError("Tenant not found")
        db = get_db()
        doc = await db.tenants.find_one({"tenant_id": tenant_id})
        integrations = normalize_integrations((doc or {}).get("integration_configs"))

        inv = integrations["inventory"]
        masked_sources = []
        for src in inv.get("sources", []):
            masked_sources.append(
                {
                    **src,
                    "config": IntegrationService.mask_config(
                        "inventory", src.get("provider", "stub"), src.get("config") or {}
                    ),
                }
            )
        integrations["inventory"]["sources"] = masked_sources

        for cat in ("crm", "calendar"):
            block = integrations[cat]
            integrations[cat] = {
                **block,
                "config": IntegrationService.mask_config(
                    cat, block.get("provider", "internal"), block.get("config") or {}
                ),
            }

        return {
            "tenant_id": tenant_id,
            "org_name": ctx.org_name,
            "integrations": integrations,
            "tier": (doc or {}).get("tier", "free"),
            "used_minutes": (doc or {}).get("used_minutes", 0.0),
            "allowed_minutes": (doc or {}).get("allowed_minutes", 30),
            "status": (doc or {}).get("status", "active"),
            "settings": {
                "system_prompt": ctx.settings.system_prompt,
                "company_description": ctx.settings.company_description,
                "webhook_url": ctx.settings.webhook_url,
                "rate_limit_per_minute": ctx.settings.rate_limit_per_minute,
            },
        }

    @staticmethod
    async def save_settings(tenant_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        db = get_db()
        doc = await db.tenants.find_one({"tenant_id": tenant_id})
        if not doc:
            raise ValueError("Tenant not found")

        settings = doc.get("settings") or {}
        incoming = payload.get("settings") or payload
        if "system_prompt" in incoming:
            settings["system_prompt"] = incoming["system_prompt"]
        if "company_description" in incoming:
            settings["company_description"] = incoming["company_description"]
        if "webhook_url" in incoming:
            settings["webhook_url"] = incoming["webhook_url"]
        if "rate_limit_per_minute" in incoming:
            settings["rate_limit_per_minute"] = int(incoming["rate_limit_per_minute"])

        await db.tenants.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "settings": settings,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        return await IntegrationService.get_admin_view(tenant_id)

    @staticmethod
    async def reset_agent_prompt(tenant_id: str) -> Dict[str, Any]:
        from backend.agent.prompts import build_tenant_system_prompt

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            raise ValueError("Tenant not found")
        description = ctx.settings.company_description or ""
        prompt = build_tenant_system_prompt(ctx.org_name or tenant_id, description)
        return await IntegrationService.save_settings(tenant_id, {"system_prompt": prompt})

    @staticmethod
    async def save_integrations(tenant_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        db = get_db()
        doc = await db.tenants.find_one({"tenant_id": tenant_id})
        if not doc:
            raise ValueError("Tenant not found")

        existing = normalize_integrations(doc.get("integration_configs"))
        incoming = payload.get("integrations") or payload

        # Inventory — multiple sources
        if "inventory" in incoming:
            inv_in = incoming["inventory"]
            existing_inv = existing["inventory"]
            existing_inv["enabled"] = bool(inv_in.get("enabled", existing_inv.get("enabled", True)))
            new_sources = []
            existing_by_id = {s["id"]: s for s in existing_inv.get("sources", []) if s.get("id")}

            for src in inv_in.get("sources", []):
                provider_id = (src.get("provider") or "stub").lower()
                src_id = src.get("id") or str(uuid.uuid4())
                prev = existing_by_id.get(src_id, {})
                config = IntegrationService.prepare_config_for_storage(
                    "inventory",
                    provider_id,
                    src.get("config") or {},
                    prev.get("config") or {},
                )
                new_sources.append(
                    {
                        "id": src_id,
                        "enabled": bool(src.get("enabled", True)),
                        "provider": provider_id,
                        "priority": int(src.get("priority", 0)),
                        "label": src.get("label") or provider_id,
                        "config": config,
                    }
                )
            existing_inv["sources"] = sorted(new_sources, key=lambda s: s.get("priority", 0))
            existing["inventory"] = existing_inv

        for cat in ("crm", "calendar"):
            if cat not in incoming:
                continue
            block_in = incoming[cat]
            provider_id = (block_in.get("provider") or "internal").lower()
            prev = existing.get(cat) or {}
            config = IntegrationService.prepare_config_for_storage(
                cat,
                provider_id,
                block_in.get("config") or {},
                prev.get("config") or {},
            )
            existing[cat] = {
                "enabled": bool(block_in.get("enabled", True)),
                "provider": provider_id,
                "config": config,
            }

        _disable_demo_stub_sources(existing)

        org_name = doc.get("org_name") or tenant_id
        current_prompt = (doc.get("settings") or {}).get("system_prompt") or ""
        if _integrations_use_sql(existing):
            await _maybe_refresh_tenant_prompt(db, tenant_id, org_name, current_prompt)

        await db.tenants.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "integration_configs": existing,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        return await IntegrationService.get_admin_view(tenant_id)

    @staticmethod
    async def test_connection(
        tenant_id: str,
        category: str,
        provider_id: str,
        config: Dict[str, Any],
        existing_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from backend.adapters.factory import AdapterFactory

        resolved = IntegrationService.prepare_config_for_storage(
            category, provider_id, config, existing_config or {}
        )
        resolved = IntegrationService.resolve_secrets(category, provider_id, resolved)

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            raise ValueError("Tenant not found")

        try:
            if category == "inventory":
                adapter = AdapterFactory.build_inventory_source(provider_id, resolved, ctx)
                sample = await adapter.list_products("products")
                preview = sample[:500] + ("..." if len(sample) > 500 else "")
                return {"ok": True, "message": "Connection successful.", "preview": preview}
            if category == "crm":
                adapter = AdapterFactory.crm_from_config(provider_id, resolved, ctx)
                sample = await adapter.search_company("test")
                return {"ok": True, "message": "CRM connection successful.", "preview": sample[:500]}
            if category == "calendar":
                adapter = AdapterFactory.calendar_from_config(provider_id, resolved, ctx)
                if adapter is None:
                    return {"ok": True, "message": "Internal calendar — no external connection needed."}
                ok = await adapter.check_availability("2099-01-01", "9:00 AM")
                return {"ok": True, "message": "Calendar connection successful.", "available": ok}
            return {"ok": False, "message": f"Unknown category: {category}"}
        except Exception as e:
            logger.exception("Integration test failed for %s/%s", category, provider_id)
            return {"ok": False, "message": str(e)}

    @staticmethod
    async def discover_schema(
        tenant_id: str,
        category: str,
        provider_id: str,
        config: Dict[str, Any],
        existing_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from backend.integrations.sql_discovery import (
            SQL_PROVIDERS,
            build_discovery_response,
            discover_sql_schema,
        )

        provider_id = provider_id.lower()
        if provider_id not in SQL_PROVIDERS:
            raise ValueError(f"Schema discovery is only available for SQL providers (got {provider_id})")

        resolved = IntegrationService.prepare_config_for_storage(
            category, provider_id, config, existing_config or {}
        )
        resolved = IntegrationService.resolve_secrets(category, provider_id, resolved)

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            raise ValueError("Tenant not found")

        tables = await discover_sql_schema(provider_id, resolved)
        if not tables:
            return {
                "ok": True,
                "message": "Connected, but no tables found in the selected schema/database.",
                "tables": [],
                "suggested_table_map": {},
                "roles": [],
            }
        result = build_discovery_response(category, provider_id, tables)
        result["message"] = f"Found {len(tables)} table(s). Pick which ones the agent can use below."
        return result
