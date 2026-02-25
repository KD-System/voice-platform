"""YandexGPT LLM (streaming с поддержкой посентенциальной выдачи)"""
import json
import aiohttp
from .base import BaseLLM


class YandexLLM(BaseLLM):
    URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"

    def __init__(self, api_key: str, folder_id: str,
                 model: str = None, temperature: float = 0.3, max_tokens: int = 500):
        self.api_key = api_key
        self.folder_id = folder_id
        self.model = model or f"gpt://{folder_id}/yandexgpt/rc"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def chat(self, messages: list) -> str:
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Project": self.folder_id,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        full_response = ""
        async with session.post(
            self.URL, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"LLM error {resp.status}: {error[:200]}")
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            full_response += content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        return full_response.strip()

    async def chat_stream_sentences(self, messages: list):
        """
        Стриминг LLM с выдачей по предложениям.
        Yields: sentence (str) по мере готовности.
        Последний yield — остаток текста.
        """
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Project": self.folder_id,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        buffer = ""
        # Разделители предложений (включая армянские)
        sentence_enders = '.!?։:;'

        async with session.post(
            self.URL, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"LLM error {resp.status}: {error[:200]}")
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            buffer += content
                            # Проверяем есть ли завершённое предложение
                            for i, ch in enumerate(buffer):
                                if ch in sentence_enders and i > 5:
                                    sentence = buffer[:i + 1].strip()
                                    buffer = buffer[i + 1:].strip()
                                    if sentence:
                                        yield sentence
                                    break
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

        # Отдаём остаток
        if buffer.strip():
            yield buffer.strip()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
