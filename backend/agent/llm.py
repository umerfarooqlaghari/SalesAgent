from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI

from backend.config import settings


# P07: a fresh ChatGoogleGenerativeAI (and its HTTP client / connection pool) was
# being constructed on every sdr_node pass and twice more in the voice fast path.
# The instance is stateless for our usage, so memoise per configuration.
@lru_cache(maxsize=16)
def _build_chat_llm(streaming: bool, temperature: float, max_retries: int) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        api_key=settings.GEMINI_API_KEY,
        model=settings.GEMINI_MODEL,
        temperature=temperature,
        streaming=streaming,
        max_retries=max_retries,
    )


def get_chat_llm(
    *,
    streaming: bool = False,
    temperature: float = 0.3,
    max_retries: int = 2,
) -> ChatGoogleGenerativeAI:
    return _build_chat_llm(streaming, temperature, max_retries)
