"""Multi-tenant mapped-table vocabulary tests."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations.tenant_inventory import (
    format_mapped_entities_for_prompt,
    inventory_vocab,
    is_inventory_question_for_tenant,
)


async def test_vocab_from_mapped_labels_not_global_products():
    mapped = [
        {"table": "show_productions", "label": "Productions", "role": "productions", "enabled": True},
        {"table": "scenic_sets", "label": "Sets", "role": "sets", "enabled": True},
    ]
    with patch(
        "backend.integrations.tenant_inventory.load_inventory_mappings",
        new=AsyncMock(return_value=mapped),
    ):
        vocab = await inventory_vocab("t1")
    assert "productions" in vocab or "production" in vocab
    assert "sets" in vocab or "set" in vocab
    # Must not require a products table to exist
    assert "catalog" in vocab
    print("✓ vocab built from tenant mapped labels")


async def test_inventory_question_uses_tenant_vocab():
    with patch(
        "backend.integrations.tenant_inventory.inventory_vocab",
        new=AsyncMock(return_value={"productions", "production", "sets", "set", "catalog"}),
    ), patch(
        "backend.integrations.tenant_inventory.tenant_has_sql_inventory",
        new=AsyncMock(return_value=True),
    ):
        assert await is_inventory_question_for_tenant("t1", "What productions do you run?") is True
        assert await is_inventory_question_for_tenant("t1", "Who are you?") is False
    print("✓ inventory intent follows tenant vocab")


def test_prompt_lists_mapped_entities():
    text = format_mapped_entities_for_prompt(
        [{"label": "Productions", "role": "productions", "table": "show_productions"}]
    )
    assert "Productions" in text
    assert "show_productions" in text
    assert "approved inventory tables" in text
    print("✓ prompt hint lists approved tables")


async def main():
    await test_vocab_from_mapped_labels_not_global_products()
    await test_inventory_question_uses_tenant_vocab()
    test_prompt_lists_mapped_entities()
    print("\nAll tenant inventory tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
