"""Voice FAQ fast-path unit tests (no live Gemini required)."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations import knowledge_cache as kc
from backend.integrations.voice_fastpath import _spoken_from_knowledge, try_voice_faq_answer


def test_spoken_stops_at_prompt_markers():
    knowledge = (
        "[Services] Alpha Devs builds AI ERP and computer vision.\n"
        "--- COMPANY IDENTITY ---\n"
        "* Who we are: ignore this for voice"
    )
    spoken = _spoken_from_knowledge(knowledge)
    assert "AI ERP" in spoken
    assert "COMPANY IDENTITY" not in spoken
    assert "let me check" not in spoken.lower()
    print("✓ spoken summary strips prompt markers")


async def test_faq_fastpath_instant_from_cache():
    tid = "alpha_devs_test_fp"
    kc._CACHE[tid] = {
        "text": "[Services] We offer AI ERP, SaaS, and ed-tech.\n[Packages] Custom builds.",
        "expires_at": 9e12,
        "fetched_at": 0,
        "org_name": "Alpha Devs",
    }
    with patch("backend.integrations.voice_fastpath.get_chat_llm") as llm:
        ans = await try_voice_faq_answer(tid, "what are your services and packages?")
    llm.assert_not_called()
    assert ans is not None
    assert "AI ERP" in ans
    assert "let me check" not in ans.lower()
    kc.invalidate_knowledge(tid)
    print("✓ FAQ fast-path uses cache without LLM")


async def test_non_faq_returns_none():
    ans = await try_voice_faq_answer("t", "book an appointment tomorrow at 3")
    assert ans is None
    print("✓ non-FAQ falls through to full agent")


async def main():
    test_spoken_stops_at_prompt_markers()
    await test_faq_fastpath_instant_from_cache()
    await test_non_faq_returns_none()
    print("\nAll voice fast-path tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
