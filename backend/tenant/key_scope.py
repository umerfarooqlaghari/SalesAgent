"""Tracks whether the current request authenticated with a secret or publishable key."""
from __future__ import annotations

import contextvars
from typing import Literal

KeyScope = Literal["secret", "publishable", "jwt"]

# S23: every current resolution path (resolve_tenant_by_api_key, JWT auth)
# explicitly calls set_key_scope, so this default only matters for a FUTURE
# path that forgets to. Default to least privilege so that mistake fails
# closed instead of silently granting a browser-embedded key full admin scope.
current_key_scope: contextvars.ContextVar[KeyScope] = contextvars.ContextVar(
    "current_key_scope", default="publishable"
)


def set_key_scope(scope: KeyScope) -> contextvars.Token:
    return current_key_scope.set(scope)


def get_key_scope() -> KeyScope:
    return current_key_scope.get()


def reset_key_scope(token: contextvars.Token) -> None:
    current_key_scope.reset(token)
