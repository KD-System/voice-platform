"""Базовый интерфейс LLM для всей платформы"""
from abc import ABC, abstractmethod


class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: list) -> str:
        """Отправить сообщения, получить полный ответ"""
        pass

    async def chat_stream_sentences(self, messages: list):
        """Стриминг по предложениям. Дефолт: вызывает chat() и отдаёт целиком."""
        result = await self.chat(messages)
        if result:
            yield result

    @abstractmethod
    async def close(self):
        pass
