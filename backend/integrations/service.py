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


class SecretDecryptionError(RuntimeError):
    """A stored credential exists but cannot be decrypted with the current key."""


def _prompt_has_stale_catalog(prompt: Optional[str]) -> bool:
    try:
        from backend.agent.prompts import looks_like_hardcoded_catalog

        return bool(prompt) and looks_like_hardcoded_catalog(prompt)
    except Exception:
        return False


def _first_error_line(sample: str) -> str:
    """Pull the most useful line out of an adapter's error text for the admin UI."""
    for line in (sample or "").splitlines():
        low = line.lower()
        if "error" in low or "failed" in low or "incorrect" in low:
            return line.strip()[:300]
    return (sample or "Connection test returned no usable rows.").strip()[:300]


def _invalidate(tenant_id: str) -> None:
    """Drop cached tenant state after a write (P01/P02 caches)."""
    from backend.tenant.registry import invalidate_tenant

    invalidate_tenant(tenant_id)
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
    _invalidate(tenant_id)
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
                except RuntimeError as e:
                    # S08: silently falling back to plaintext storage for a
                    # password/API key because ENCRYPTION_KEY wasn't configured
                    # was worse than refusing the save outright.
                    raise ValueError(
                        f"Cannot save {key}: ENCRYPTION_KEY is not configured on the server."
                    ) from e
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

        # A16: any config/existing key that isn't one of the provider's known
        # fields (e.g. mapped_tables, admin-authored schema metadata) used to
        # be silently dropped on every save because the loop above only ever
        # writes keys declared on the provider. Pass those through untouched.
        known_keys = {f.key for f in provider.fields}
        for source in (existing, config):
            for key, value in source.items():
                if key in known_keys or key.endswith(SECRET_SUFFIX):
                    continue
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
                    # This used to fall back to "". A key-management problem
                    # (ENCRYPTION_KEY unset, rotated, or different between
                    # environments) therefore reached the database as an EMPTY
                    # password and came back as "your credentials are incorrect",
                    # sending everyone hunting a credential bug that did not exist.
                    logger.error(
                        "Failed to decrypt %s for %s/%s: %s", key, category, provider_id, e
                    )
                    raise SecretDecryptionError(
                        f"Stored {key} for {provider_id} could not be decrypted. "
                        "ENCRYPTION_KEY has most likely changed since it was saved - "
                        f"re-enter the {key} under Integrations and save again."
                    ) from e
                del resolved[enc_key]
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
            "prompt_warning": (
                "This prompt lists specific products/prices. The live catalogue will "
                "override it, but the stale list can still confuse the agent — click "
                "\"Reset prompt for my company\" to regenerate it from your tables."
                if _prompt_has_stale_catalog(ctx.settings.system_prompt) else None
            ),
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
        # S21: rate_limit_per_minute is tier-controlled (see billing/routes.py)
        # and must not be tenant-writable through the settings save endpoint —
        # a tenant could otherwise set it to an arbitrary value regardless of
        # their plan.

        await db.tenants.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "settings": settings,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        _invalidate(tenant_id)
        return await IntegrationService.get_admin_view(tenant_id)

    @staticmethod
    async def reset_agent_prompt(tenant_id: str) -> Dict[str, Any]:   # noqa: D401
        """
        Regenerate the tenant's system prompt from their connected data.

        Previously this returned a fixed template that knew only the org name and
        the free-text description — so tenants hand-wrote prompts that embedded a
        snapshot of the catalogue, and the agent then answered from that snapshot
        instead of the live tables.
        """
        from backend.agent.prompt_builder import generate_tenant_system_prompt

        result = await generate_tenant_system_prompt(tenant_id)
        saved = await IntegrationService.save_settings(
            tenant_id, {"system_prompt": result["prompt"]}
        )
        if isinstance(saved, dict):
            saved["prompt_generated"] = result.get("generated", False)
            saved["prompt_source"] = result.get("reason", "")
            saved["prompt_tables"] = result.get("tables", [])
        return saved

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

        hub_sync = await IntegrationService.sync_to_adapter_hub(tenant_id, existing)
        view = await IntegrationService.get_admin_view(tenant_id)
        _invalidate(tenant_id)

        if hub_sync:
            view["adapter_hub_sync"] = hub_sync
        return view

    @staticmethod
    async def sync_to_adapter_hub(
        tenant_id: str,
        integrations: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Push inventory + CRM SQL mappings into adapter-hub and sync rows for RAG."""
        from backend.integrations.adapter_hub_client import sync_tenant_inventory
        from backend.integrations.table_map_util import get_mapped_tables, parse_table_map_raw

        if integrations is None:
            try:
                integrations = await IntegrationService.get_tenant_integrations(tenant_id)
            except ValueError:
                return {"ok": False, "error": "Tenant not found"}

        # A15: this used to resolve ONE shared connection (overwritten on each
        # loop iteration) and sync every source's mapped tables through
        # whichever connection happened to be resolved last — a tenant with a
        # separate warehouse DB and CRM DB had the warehouse's tables silently
        # synced through the CRM's connection (or vice-versa). Each source now
        # syncs through its own resolved connection.
        sources: List[tuple] = []  # (provider_id, resolved_config, mapped_tables)

        inv = integrations.get("inventory") or {}
        for src in inv.get("sources") or []:
            if not src.get("enabled", True):
                continue
            pid = (src.get("provider") or "").lower()
            if pid not in SQL_PROVIDERS:
                continue
            resolved = IntegrationService.resolve_secrets(
                "inventory", pid, src.get("config") or {}
            )
            tm = parse_table_map_raw(resolved)
            mapped = get_mapped_tables(tm, "inventory")
            if mapped:
                sources.append((pid, resolved, mapped))

        crm = integrations.get("crm") or {}
        crm_provider = (crm.get("provider") or "").lower()
        if crm.get("enabled", True) and crm_provider in SQL_PROVIDERS:
            crm_resolved = IntegrationService.resolve_secrets(
                "crm", crm_provider, crm.get("config") or {}
            )
            tm = parse_table_map_raw(crm_resolved)
            mapped = get_mapped_tables(tm, "crm")
            if mapped:
                sources.append((crm_provider, crm_resolved, mapped))

        if not sources:
            return {"ok": False, "skipped": True, "error": "No SQL inventory/CRM sources to sync"}

        results = []
        total_synced = 0
        errors = []
        for provider_id, resolved, mapped in sources:
            # Dedupe by table name within this source only.
            seen = set()
            unique_mapped = []
            for mt in mapped:
                key = mt.get("table")
                if not key or key in seen:
                    continue
                seen.add(key)
                unique_mapped.append(mt)

            try:
                res = await sync_tenant_inventory(tenant_id, provider_id, resolved, unique_mapped)
                results.append(res)
                total_synced += int(res.get("synchronized_count") or 0)
                if not res.get("ok"):
                    errors.append(res.get("error"))
            except Exception as e:
                logger.warning("Adapter-hub sync failed for %s (%s): %s", tenant_id, provider_id, e)
                errors.append(str(e))
                results.append({"ok": False, "error": str(e)})

        return {
            "ok": not errors,
            "synchronized_count": total_synced,
            "results": results,
            "error": "; ".join(e for e in errors if e) or None,
        }

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
        try:
            resolved = IntegrationService.resolve_secrets(category, provider_id, resolved)
        except SecretDecryptionError as e:
            return {"ok": False, "message": str(e), "preview": ""}

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            raise ValueError("Tenant not found")

        try:
            if category == "inventory":
                adapter = AdapterFactory.build_inventory_source(provider_id, resolved, ctx)
                sample = await adapter.list_products("products")
                preview = sample[:500] + ("..." if len(sample) > 500 else "")

                # The adapters return failures as text instead of raising, so a
                # connection that authenticates but cannot read a single table
                # used to be reported as "Connection successful."
                from backend.integrations.catalog_cache import _looks_like_error

                if _looks_like_error(sample):
                    return {
                        "ok": False,
                        "message": _first_error_line(sample),
                        "preview": preview,
                    }
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
