import re

_TOKEN = re.compile(r"[a-z0-9]+")


def token_set(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def score_text_overlap(query: str, document: str) -> float:
    q = token_set(query)
    if not q:
        return 0.0
    c = token_set(document)
    if not c:
        return 0.0
    return len(q & c) / len(q)
