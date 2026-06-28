from langchain_google_genai import ChatGoogleGenerativeAI

from backend.config import settings


def get_chat_llm(*, streaming: bool = False, temperature: float = 0.3) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        api_key=settings.GEMINI_API_KEY,
        model=settings.GEMINI_MODEL,
        temperature=temperature,
        streaming=streaming,
        max_retries=2,
    )
