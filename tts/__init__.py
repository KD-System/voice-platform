from .base import BaseTTS


def get_tts(provider: str, **kwargs) -> BaseTTS:
    """Фабрика TTS провайдеров."""
    if provider == "yandex":
        from .yandex import YandexTTS
        return YandexTTS(**kwargs)
    elif provider == "zvukogram":
        from .zvukogram import ZvukogramTTS
        return ZvukogramTTS(**kwargs)
    elif provider == "elevenlabs":
        from .elevenlabs import ElevenLabsTTS
        return ElevenLabsTTS(**kwargs)
    else:
        raise ValueError(f"Unknown TTS provider: {provider}. Available: yandex, zvukogram, elevenlabs")
