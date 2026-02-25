from .base import BaseASR


def get_asr(provider: str, **kwargs) -> BaseASR:
    """Фабрика ASR провайдеров."""
    if provider == "yandex":
        from .yandex import YandexASR
        return YandexASR(**kwargs)
    elif provider == "triton_armenian":
        from .triton_armenian import TritonArmenianASR
        return TritonArmenianASR(**kwargs)
    else:
        raise ValueError(f"Unknown ASR provider: {provider}. Available: yandex, triton_armenian")
