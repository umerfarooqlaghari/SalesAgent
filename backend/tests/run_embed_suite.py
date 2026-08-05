"""Run embed/cache/latency test suite (mocked — no live Mongo/LLM required)."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root not in sys.path:
    sys.path.insert(0, root)


def _run_main(mod) -> None:
    fn = getattr(mod, "main")
    if inspect.iscoroutinefunction(fn):
        asyncio.run(fn())
    else:
        result = fn()
        if inspect.isawaitable(result):
            asyncio.run(result)


def main() -> None:
    for name in (
        "backend.tests.test_catalog_cache",
        "backend.tests.test_embed_api",
        "backend.tests.test_latency_path",
    ):
        mod = importlib.import_module(name)
        _run_main(mod)
        print()
    print("=== Embed / latency / cache suite passed ===")


if __name__ == "__main__":
    main()
