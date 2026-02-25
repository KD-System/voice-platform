"""Базовый интерфейс ASR для всей платформы"""
from abc import ABC, abstractmethod


class BaseASR(ABC):
    @abstractmethod
    async def recognize(self, audio_pcm: bytes, sample_rate: int = 8000) -> dict:
        """
        Распознать речь из PCM-аудио.
        
        Returns:
            {
                "text": "распознанный текст",
                "confidence": 0.95,
                "language": "hy-AM"
            }
        """
        pass

    @abstractmethod
    async def close(self):
        pass
