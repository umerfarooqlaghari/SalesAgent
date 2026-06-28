from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.auth.security import (
    create_access_token,
    generate_api_key,
    hash_password,
    verify_password,
)
from backend.database import get_db
from backend.integrations.normalize import normalize_integrations
from backend.tenant.secrets import hash_api_key

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class UserSession:
    user_id: str
    email: str
    name: str
    role: str
    tenant_id: Optional[str]
    org_name: Optional[str]


def _slugify_org(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:32]
    return base or "org"


async def create_tenant(org_name: str, owner_email: str) -> tuple[str, str]:
    """Create tenant + return (tenant_id, plaintext_api_key)."""
    db = get_db()
    tenant_id = f"{_slugify_org(org_name)}_{uuid.uuid4().hex[:8]}"
    api_key = generate_api_key()

    from backend.agent.prompts import build_tenant_system_prompt

    await db.tenants.insert_one(
        {
            "tenant_id": tenant_id,
            "org_name": org_name.strip(),
            "owner_email": owner_email.lower().strip(),
            "api_key_hash": hash_api_key(api_key),
            "status": "active",
            "integration_configs": normalize_integrations({}),
            "settings": {
                "system_prompt": build_tenant_system_prompt(org_name),
                "company_description": None,
                "webhook_url": None,
                "rate_limit_per_minute": 120,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return tenant_id, api_key


async def register_client(
    org_name: str,
    email: str,
    password: str,
    name: str = "",
) -> Dict[str, Any]:
    db = get_db()
    email = email.lower().strip()
    if not EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if not org_name.strip():
        raise ValueError("Organization name is required")

    existing = await db.users.find_one({"email": email})
    if existing:
        raise ValueError("An account with this email already exists")

    tenant_id, api_key = await create_tenant(org_name, email)
    user_id = str(uuid.uuid4())

    await db.users.insert_one(
        {
            "user_id": user_id,
            "email": email,
            "name": name.strip() or org_name.strip(),
            "password_hash": hash_password(password),
            "role": "tenant_admin",
            "tenant_id": tenant_id,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    token = create_access_token(
        {
            "sub": user_id,
            "email": email,
            "role": "tenant_admin",
            "tenant_id": tenant_id,
        }
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "user_id": user_id,
            "email": email,
            "name": name.strip() or org_name.strip(),
            "role": "tenant_admin",
            "tenant_id": tenant_id,
            "org_name": org_name.strip(),
        },
        "api_key": api_key,
        "message": "Save your API key now — it will not be shown again.",
    }


async def login_user(email: str, password: str) -> Dict[str, Any]:
    db = get_db()
    email = email.lower().strip()
    user = await db.users.find_one({"email": email, "status": "active"})
    if not user or not verify_password(password, user.get("password_hash", "")):
        raise ValueError("Invalid email or password")

    org_name = None
    if user.get("tenant_id"):
        tenant = await db.tenants.find_one({"tenant_id": user["tenant_id"]})
        org_name = tenant.get("org_name") if tenant else user["tenant_id"]

    token = create_access_token(
        {
            "sub": user["user_id"],
            "email": user["email"],
            "role": user.get("role", "tenant_admin"),
            "tenant_id": user.get("tenant_id"),
        }
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "name": user.get("name", ""),
            "role": user.get("role", "tenant_admin"),
            "tenant_id": user.get("tenant_id"),
            "org_name": org_name,
        },
    }


async def get_user_session(user_id: str) -> Optional[UserSession]:
    db = get_db()
    user = await db.users.find_one({"user_id": user_id, "status": "active"})
    if not user:
        return None
    org_name = None
    if user.get("tenant_id"):
        tenant = await db.tenants.find_one({"tenant_id": user["tenant_id"]})
        org_name = tenant.get("org_name") if tenant else None
    return UserSession(
        user_id=user["user_id"],
        email=user["email"],
        name=user.get("name", ""),
        role=user.get("role", "tenant_admin"),
        tenant_id=user.get("tenant_id"),
        org_name=org_name,
    )


async def regenerate_api_key(user_id: str) -> str:
    db = get_db()
    user = await db.users.find_one({"user_id": user_id, "status": "active"})
    if not user or not user.get("tenant_id"):
        raise ValueError("No tenant associated with this user")
    api_key = generate_api_key()
    await db.tenants.update_one(
        {"tenant_id": user["tenant_id"]},
        {
            "$set": {
                "api_key_hash": hash_api_key(api_key),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    return api_key


async def seed_super_admin() -> None:
    db = get_db()
    from backend.config import settings

    email = settings.SUPER_ADMIN_EMAIL.lower().strip()
    existing = await db.users.find_one({"email": email})
    if existing:
        return

    user_id = str(uuid.uuid4())
    await db.users.insert_one(
        {
            "user_id": user_id,
            "email": email,
            "name": "Super Admin",
            "password_hash": hash_password(settings.SUPER_ADMIN_PASSWORD),
            "role": "super_admin",
            "tenant_id": None,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def list_all_tenants() -> List[Dict[str, Any]]:
    db = get_db()
    tenants = []
    async for doc in db.tenants.find({}).sort([("created_at", -1)]):
        tid = doc["tenant_id"]
        tenants.append(
            {
                "tenant_id": tid,
                "org_name": doc.get("org_name", tid),
                "status": doc.get("status", "active"),
                "owner_email": doc.get("owner_email"),
                "created_at": doc.get("created_at"),
                "lead_count": await db.leads.count_documents({"tenant_id": tid}),
                "order_count": await db.orders.count_documents({"tenant_id": tid}),
                "appointment_count": await db.appointments.count_documents({"tenant_id": tid}),
                "conversation_count": await db.conversations.count_documents({"tenant_id": tid}),
            }
        )
    return tenants


async def platform_stats() -> Dict[str, Any]:
    db = get_db()
    return {
        "tenant_count": await db.tenants.count_documents({}),
        "user_count": await db.users.count_documents({}),
        "lead_count": await db.leads.count_documents({}),
        "order_count": await db.orders.count_documents({}),
        "appointment_count": await db.appointments.count_documents({}),
        "conversation_count": await db.conversations.count_documents({}),
    }
