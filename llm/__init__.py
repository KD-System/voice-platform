from .base import BaseLLM


def get_llm(provider: str, **kwargs) -> BaseLLM:
    """Фабрика LLM провайдеров."""
    if provider == "yandex":
        from .yandex import YandexLLM
        return YandexLLM(**kwargs)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Available: yandex")
