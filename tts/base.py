"""Базовый интерфейс TTS для всей платформы"""
from abc import ABC, abstractmethod


class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> dict:
        """
        Синтезировать речь из текста.
        
        Returns:
            {
                "audio": b"pcm_bytes",
                "sample_rate": 48000,
                "format": "pcm16"
            }
        """
        pass

    @abstractmethod
    async def close(self):
        pass
