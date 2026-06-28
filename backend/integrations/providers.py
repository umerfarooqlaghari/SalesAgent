"""
Provider registry — drives Admin UI forms and adapter factory routing.
Add new systems here; no hardcoding per client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

FieldType = Literal["text", "password", "number", "boolean", "select", "json", "textarea"]


@dataclass(frozen=True)
class ProviderField:
    key: str
    label: str
    field_type: FieldType = "text"
    required: bool = False
    placeholder: str = ""
    help_text: str = ""
    options: tuple[tuple[str, str], ...] = ()  # (value, label)
    default: Any = None


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    label: str
    category: str  # inventory | crm | calendar
    description: str = ""
    fields: tuple[ProviderField, ...] = ()
    secret_fields: tuple[str, ...] = ()
    supports_read_only: bool = True
    supports_write: bool = True


SQL_TABLE_MAP_FIELD = ProviderField(
    key="table_map",
    label="Table & column mapping (JSON)",
    field_type="json",
    required=True,
    placeholder='{"products_table":"products","products_columns":{"name":"name","price":"price","stock":"stock_quantity","description":"description"},"orders_table":"orders","orders_columns":{"id":"id","email":"customer_email","phone":"customer_phone","status":"status","total":"total_price","items":"items"}}',
    help_text="Maps your DB tables/columns to what the agent expects. Read-only mode uses SELECT only.",
)

SQL_CONNECTION_FIELDS: tuple[ProviderField, ...] = (
    ProviderField("host", "Host", required=True, placeholder="db.example.com or RDS endpoint"),
    ProviderField("port", "Port", field_type="number", placeholder="5432"),
    ProviderField("database", "Database name", required=True),
    ProviderField("username", "Username", required=True),
    ProviderField("password", "Password", field_type="password", required=True),
    ProviderField(
        "schema",
        "Schema (optional)",
        placeholder="public / dbo",
        help_text="SQL Server default: dbo. Postgres default: public.",
    ),
    ProviderField(
        "ssl",
        "Use SSL/TLS",
        field_type="boolean",
        default=True,
        help_text="Recommended for RDS and cloud databases.",
    ),
    ProviderField(
        "read_only",
        "Read-only access",
        field_type="boolean",
        default=True,
        help_text="When enabled, the agent can only SELECT — no inserts or updates.",
    ),
)

SQL_BASE_FIELDS: tuple[ProviderField, ...] = SQL_CONNECTION_FIELDS + (SQL_TABLE_MAP_FIELD,)

CRM_TABLE_MAP_FIELD = ProviderField(
    key="table_map",
    label="CRM table mapping (JSON)",
    field_type="json",
    required=True,
    placeholder='{"companies_table":"companies","companies_columns":{"company":"name","status":"status","fit":"is_qualified"}}',
    help_text="Maps your CRM tables/columns to what the agent expects.",
)

CALENDAR_TABLE_MAP_FIELD = ProviderField(
    key="table_map",
    label="Calendar table mapping (JSON)",
    field_type="json",
    required=True,
    placeholder='{"appointments_table":"appointments","appointments_columns":{"date":"appt_date","time":"appt_time","email":"email","phone":"phone","name":"name","status":"status"}}',
    help_text="Maps your appointment tables/columns to what the agent expects.",
)

PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        id="stub",
        label="Built-in demo catalog (SQLite)",
        category="inventory",
        description="Shared demo products — no external connection required.",
        fields=(
            ProviderField(
                "read_only",
                "Read-only",
                field_type="boolean",
                default=True,
                help_text="Demo catalog is always read-only for orders created via agent.",
            ),
        ),
        secret_fields=(),
        supports_write=False,
    ),
    ProviderDefinition(
        id="shopify",
        label="Shopify",
        category="inventory",
        description="Fetch products and orders from Shopify Admin API.",
        fields=(
            ProviderField("shop_domain", "Shop domain", required=True, placeholder="your-store.myshopify.com"),
            ProviderField("access_token", "Admin API access token", field_type="password", required=True),
            ProviderField(
                "api_version",
                "API version",
                placeholder="2024-01",
                default="2024-01",
            ),
            ProviderField(
                "read_only",
                "Read-only (no draft orders)",
                field_type="boolean",
                default=True,
            ),
        ),
        secret_fields=("access_token",),
    ),
    ProviderDefinition(
        id="postgres",
        label="PostgreSQL",
        category="inventory",
        description="Self-hosted Postgres or any Postgres-compatible DB (including RDS PostgreSQL).",
        fields=SQL_BASE_FIELDS,
        secret_fields=("password",),
    ),
    ProviderDefinition(
        id="sqlserver",
        label="Microsoft SQL Server",
        category="inventory",
        description="SQL Server or Azure SQL / RDS SQL Server.",
        fields=SQL_BASE_FIELDS,
        secret_fields=("password",),
    ),
    ProviderDefinition(
        id="mysql",
        label="MySQL / MariaDB",
        category="inventory",
        description="MySQL, MariaDB, or RDS MySQL.",
        fields=SQL_BASE_FIELDS,
        secret_fields=("password",),
    ),
    ProviderDefinition(
        id="internal",
        label="Alpha CRM (MongoDB)",
        category="crm",
        description="Use leads stored in this console — default.",
        fields=(),
        secret_fields=(),
        supports_write=True,
    ),
    ProviderDefinition(
        id="postgres",
        label="PostgreSQL (CRM tables)",
        category="crm",
        description="Read/search company records from your Postgres tables.",
        fields=SQL_CONNECTION_FIELDS + (CRM_TABLE_MAP_FIELD,),
        secret_fields=("password",),
    ),
    ProviderDefinition(
        id="sqlserver",
        label="SQL Server (CRM tables)",
        category="crm",
        description="Read/search company records from SQL Server.",
        fields=SQL_CONNECTION_FIELDS + (CRM_TABLE_MAP_FIELD,),
        secret_fields=("password",),
    ),
    ProviderDefinition(
        id="rest",
        label="REST API",
        category="crm",
        description="Generic REST endpoint for CRM search/sync (configure paths and headers).",
        fields=(
            ProviderField("base_url", "Base URL", required=True, placeholder="https://api.example.com"),
            ProviderField("search_path", "Search path", placeholder="/companies/search?q={company}"),
            ProviderField("sync_path", "Sync path (POST)", placeholder="/leads"),
            ProviderField("auth_header", "Auth header name", placeholder="Authorization"),
            ProviderField("auth_token", "Auth token / API key", field_type="password"),
            ProviderField(
                "read_only",
                "Read-only",
                field_type="boolean",
                default=True,
            ),
        ),
        secret_fields=("auth_token",),
    ),
    ProviderDefinition(
        id="internal",
        label="Alpha calendar (MongoDB)",
        category="calendar",
        description="Book appointments into this console — default.",
        fields=(),
        secret_fields=(),
    ),
    ProviderDefinition(
        id="postgres",
        label="PostgreSQL (appointments)",
        category="calendar",
        description="Read/write appointments from your Postgres tables.",
        fields=SQL_CONNECTION_FIELDS + (CALENDAR_TABLE_MAP_FIELD,),
        secret_fields=("password",),
    ),
    ProviderDefinition(
        id="google",
        label="Google Calendar",
        category="calendar",
        description="Google Calendar OAuth (connect via service account JSON or access token).",
        fields=(
            ProviderField("calendar_id", "Calendar ID", required=True, placeholder="primary or email@group.calendar.google.com"),
            ProviderField("access_token", "Access token", field_type="password"),
            ProviderField(
                "service_account_json",
                "Service account JSON",
                field_type="textarea",
                help_text="Paste full JSON key for server-to-server access.",
            ),
            ProviderField("read_only", "Read-only", field_type="boolean", default=False),
        ),
        secret_fields=("access_token", "service_account_json"),
    ),
)

INTEGRATION_CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "id": "inventory",
        "label": "Inventory / POS",
        "description": "Product catalog, stock levels, and order lookups. Multiple sources merge by priority.",
        "allow_multiple": True,
    },
    {
        "id": "crm",
        "label": "CRM",
        "description": "Lead and company lookup. One active CRM source at a time.",
        "allow_multiple": False,
    },
    {
        "id": "calendar",
        "label": "Calendar",
        "description": "Availability and booking. One active calendar source at a time.",
        "allow_multiple": False,
    },
)

_PROVIDER_INDEX: Dict[tuple[str, str], ProviderDefinition] = {
    (p.category, p.id): p for p in PROVIDERS
}


def get_provider(category: str, provider_id: str) -> Optional[ProviderDefinition]:
    return _PROVIDER_INDEX.get((category, provider_id.lower()))


def list_providers(category: Optional[str] = None) -> List[ProviderDefinition]:
    if category:
        return [p for p in PROVIDERS if p.category == category]
    return list(PROVIDERS)


def provider_to_schema(provider: ProviderDefinition) -> Dict[str, Any]:
    return {
        "id": provider.id,
        "label": provider.label,
        "category": provider.category,
        "description": provider.description,
        "supports_read_only": provider.supports_read_only,
        "supports_write": provider.supports_write,
        "secret_fields": list(provider.secret_fields),
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "type": f.field_type,
                "required": f.required,
                "placeholder": f.placeholder,
                "help_text": f.help_text,
                "options": [{"value": v, "label": l} for v, l in f.options],
                "default": f.default,
            }
            for f in provider.fields
        ],
    }


def build_schemas_response() -> Dict[str, Any]:
    categories = []
    for cat in INTEGRATION_CATEGORIES:
        cat_id = cat["id"]
        categories.append(
            {
                **cat,
                "providers": [provider_to_schema(p) for p in list_providers(cat_id)],
            }
        )
    return {"categories": categories}
