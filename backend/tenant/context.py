from dataclasses import dataclass, field
from typing import Any, Dict, Optional


DEFAULT_TENANT_ID = "alpha_default"


@dataclass
class TenantSettings:
    system_prompt: Optional[str] = None
    company_description: Optional[str] = None
    webhook_url: Optional[str] = None
    rate_limit_per_minute: int = 120

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TenantSettings":
        data = data or {}
        return cls(
            system_prompt=data.get("system_prompt"),
            company_description=data.get("company_description"),
            webhook_url=data.get("webhook_url"),
            rate_limit_per_minute=int(data.get("rate_limit_per_minute", 120)),
        )


@dataclass
class IntegrationConfigs:
    crm: Dict[str, Any] = field(default_factory=dict)
    pos: Dict[str, Any] = field(default_factory=dict)
    calendar: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "IntegrationConfigs":
        data = data or {}
        return cls(
            crm=data.get("crm") or {},
            pos=data.get("pos") or {"provider": "stub"},
            calendar=data.get("calendar") or {},
        )

    @property
    def pos_provider(self) -> str:
        return (self.pos.get("provider") or "stub").lower()

    @property
    def crm_provider(self) -> str:
        return (self.crm.get("provider") or "none").lower()

    @property
    def calendar_provider(self) -> str:
        return (self.calendar.get("provider") or "none").lower()


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    org_name: str
    settings: TenantSettings
    integrations: IntegrationConfigs
    status: str = "active"
    integrations_raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_document(cls, doc: Dict[str, Any]) -> "TenantContext":
        return cls(
            tenant_id=doc["tenant_id"],
            org_name=doc.get("org_name", doc["tenant_id"]),
            settings=TenantSettings.from_dict(doc.get("settings")),
            integrations=IntegrationConfigs.from_dict(doc.get("integration_configs")),
            status=doc.get("status", "active"),
            integrations_raw=doc.get("integration_configs"),
        )
