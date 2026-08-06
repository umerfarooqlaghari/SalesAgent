import logging
from typing import Any, Dict, List, Optional, Union

from backend.adapters.base import CRMAdapter, CalendarAdapter, NoOpCRMAdapter, POSAdapter
from backend.adapters.composite import CompositePOSAdapter
from backend.adapters.crm_adapters import RestCRMAdapter, SqlCRMAdapter
from backend.adapters.shopify_pos import ShopifyPOSAdapter
from backend.adapters.sql_pos import SqlPOSAdapter
from backend.adapters.stub_pos import EmptyPOSAdapter, StubPOSAdapter
from backend.config import settings
from backend.integrations.service import IntegrationService, normalize_integrations
from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)

SQL_PROVIDERS = {"postgres", "sqlserver", "mysql"}


def _is_demo_tenant(tenant: TenantContext) -> bool:
    """The shared SQLite POS is demo data. Only the default tenant may see it."""
    return getattr(tenant, "tenant_id", None) == settings.DEFAULT_TENANT_ID


def _empty_or_demo(tenant: TenantContext) -> POSAdapter:
    return StubPOSAdapter(tenant) if _is_demo_tenant(tenant) else EmptyPOSAdapter(tenant)


class AdapterFactory:
    @staticmethod
    def _integrations(tenant: TenantContext) -> Dict[str, Any]:
        return normalize_integrations(tenant.integrations_raw)

    @staticmethod
    def build_inventory_source(
        provider_id: str,
        config: Dict[str, Any],
        tenant: TenantContext,
    ) -> POSAdapter:
        pid = provider_id.lower()
        if pid in ("stub", "sqlite", "none", ""):
            # T01/T02: the stub is the shared demo SQLite catalog.
            if not _is_demo_tenant(tenant):
                logger.warning(
                    "Refusing to serve the demo catalog to tenant %s (provider=%r)",
                    getattr(tenant, "tenant_id", "?"), pid,
                )
            return _empty_or_demo(tenant)
        if pid == "shopify":
            return ShopifyPOSAdapter(config, tenant)
        if pid in SQL_PROVIDERS:
            return SqlPOSAdapter(pid, config, tenant)
        # T02: fail closed. A config typo or a provider removed in a later release
        # must not silently downgrade a real tenant to the demo catalog.
        logger.error(
            "Unknown inventory provider %r for tenant %s — serving no inventory",
            pid, getattr(tenant, "tenant_id", "?"),
        )
        return _empty_or_demo(tenant)

    @staticmethod
    def pos(tenant: TenantContext) -> POSAdapter:
        integrations = AdapterFactory._integrations(tenant)
        inv = integrations.get("inventory") or {}
        if not inv.get("enabled", True):
            return _empty_or_demo(tenant)

        adapters: List[tuple[int, str, POSAdapter]] = []
        for src in inv.get("sources") or []:
            if not src.get("enabled", True):
                continue
            provider_id = (src.get("provider") or "stub").lower()
            label = src.get("label") or provider_id
            priority = int(src.get("priority", 0))
            config = IntegrationService.resolve_secrets("inventory", provider_id, src.get("config") or {})
            try:
                adapter = AdapterFactory.build_inventory_source(provider_id, config, tenant)
                adapters.append((priority, label, adapter))
            except Exception as e:
                logger.error("Failed to build inventory adapter %s: %s", label, e)

        if not adapters:
            return _empty_or_demo(tenant)
        if len(adapters) == 1:
            return adapters[0][2]
        return CompositePOSAdapter(adapters)

    @staticmethod
    def crm_from_config(
        provider_id: str,
        config: Dict[str, Any],
        tenant: TenantContext,
    ) -> CRMAdapter:
        pid = provider_id.lower()
        if pid in ("none", "", "internal"):
            return NoOpCRMAdapter()
        if pid in SQL_PROVIDERS:
            return SqlCRMAdapter(pid, config, tenant)
        if pid == "rest":
            return RestCRMAdapter(config, tenant)
        logger.warning("Unknown CRM provider '%s' — using no-op", pid)
        return NoOpCRMAdapter()

    @staticmethod
    def crm(tenant: TenantContext) -> CRMAdapter:
        integrations = AdapterFactory._integrations(tenant)
        block = integrations.get("crm") or {}
        if not block.get("enabled", True):
            return NoOpCRMAdapter()
        provider_id = (block.get("provider") or "internal").lower()
        config = IntegrationService.resolve_secrets("crm", provider_id, block.get("config") or {})
        return AdapterFactory.crm_from_config(provider_id, config, tenant)

    @staticmethod
    def calendar_from_config(
        provider_id: str,
        config: Dict[str, Any],
        tenant: TenantContext,
    ) -> Optional[CalendarAdapter]:
        pid = provider_id.lower()
        if pid in ("none", "", "internal"):
            return None
        if pid == "google":
            from backend.adapters.google_calendar import GoogleCalendarAdapter

            return GoogleCalendarAdapter(config, tenant)
        if pid in SQL_PROVIDERS:
            from backend.adapters.sql_calendar import SqlCalendarAdapter

            return SqlCalendarAdapter(pid, config, tenant)
        return None

    @staticmethod
    def calendar(tenant: TenantContext) -> Union[CalendarAdapter, None]:
        integrations = AdapterFactory._integrations(tenant)
        block = integrations.get("calendar") or {}
        if not block.get("enabled", True):
            return None
        provider_id = (block.get("provider") or "internal").lower()
        config = IntegrationService.resolve_secrets("calendar", provider_id, block.get("config") or {})
        return AdapterFactory.calendar_from_config(provider_id, config, tenant)
