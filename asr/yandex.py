"""Yandex SpeechKit ASR"""
import aiohttp
from .base import BaseASR


class YandexASR(BaseASR):
    URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

    def __init__(self, api_key: str, folder_id: str, language: str = "ru-RU"):
        self.api_key = api_key
        self.folder_id = folder_id
        self.language = language
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def recognize(self, audio_pcm: bytes, sample_rate: int = 8000) -> dict:
        session = await self._get_session()
        params = {
            "topic": "general",
            "lang": self.language,
            "folderId": self.folder_id,
            "format": "lpcm",
            "sampleRateHertz": str(sample_rate),
        }
        headers = {"Authorization": f"Api-Key {self.api_key}"}
        async with session.post(
            self.URL, params=params, headers=headers, data=audio_pcm,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                return {
                    "text": result.get("result", ""),
                    "confidence": 1.0,
                    "language": self.language,
                }
            else:
                error = await resp.text()
                raise RuntimeError(f"ASR error {resp.status}: {error[:200]}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
