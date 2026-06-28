"""
Core unit tests — no MongoDB required.
Run: python3 -m backend.tests.test_core
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.agent.intent import heuristic_intent
from backend.agent.text_utils import score_text_overlap
from backend.integrations.normalize import normalize_integrations, mask_config
from backend.integrations.providers import get_provider, list_providers


def test_heuristic_intent():
    assert heuristic_intent("I want to buy the professional package") == "Purchase"
    assert heuristic_intent("Can I cancel my order?") == "Support"
    assert heuristic_intent("What services do you offer?") == "Inquiry"
    assert heuristic_intent("") == "Inquiry"
    print("✓ heuristic intent")


def test_rag_scoring():
    assert score_text_overlap("enterprise pricing", "SaaS Enterprise License $999") > 0
    assert score_text_overlap("xyzunknown", "unrelated text") == 0
    print("✓ RAG scoring")


def test_normalize_integrations_legacy():
    legacy = {
        "pos": {"provider": "stub"},
        "crm": {"provider": "internal"},
        "calendar": {"provider": "internal"},
    }
    norm = normalize_integrations(legacy)
    assert norm["inventory"]["sources"][0]["provider"] == "stub"
    assert norm["crm"]["provider"] == "internal"
    print("✓ integration normalization (legacy)")


def test_normalize_integrations_multi_source():
    modern = {
        "inventory": {
            "enabled": True,
            "sources": [
                {"id": "a", "enabled": True, "provider": "shopify", "priority": 1, "config": {}},
                {"id": "b", "enabled": True, "provider": "postgres", "priority": 0, "config": {"read_only": True}},
            ],
        },
        "crm": {"enabled": True, "provider": "internal", "config": {}},
        "calendar": {"enabled": False, "provider": "internal", "config": {}},
    }
    norm = normalize_integrations(modern)
    assert len(norm["inventory"]["sources"]) == 2
    assert norm["calendar"]["enabled"] is False
    print("✓ integration normalization (multi-source)")


def test_provider_registry():
    inv = list_providers("inventory")
    ids = {p.id for p in inv}
    assert "shopify" in ids
    assert "postgres" in ids
    assert "sqlserver" in ids
    assert get_provider("inventory", "mysql") is not None
    print("✓ provider registry")


def test_mask_secrets():
    masked = mask_config(
        "inventory",
        "shopify",
        {"shop_domain": "x.myshopify.com", "access_token": "secret123"},
    )
    assert masked["access_token"] == "••••••••"
    assert masked["shop_domain"] == "x.myshopify.com"
    print("✓ secret masking")


def main():
    test_heuristic_intent()
    test_rag_scoring()
    test_normalize_integrations_legacy()
    test_normalize_integrations_multi_source()
    test_provider_registry()
    test_mask_secrets()
    print("\nAll core tests passed.")


if __name__ == "__main__":
    main()
