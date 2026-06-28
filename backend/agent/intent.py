import re

_PURCHASE = re.compile(r"\b(buy|purchase|order|package|sign up|i'll take|i will take)\b", re.I)
_SUPPORT = re.compile(r"\b(cancel|problem|issue|help|support|broken|refund|complaint)\b", re.I)


def heuristic_intent(text: str) -> str:
    """Fast intent routing — avoids an extra LLM call per message."""
    if _SUPPORT.search(text):
        return "Support"
    if _PURCHASE.search(text):
        return "Purchase"
    return "Inquiry"
