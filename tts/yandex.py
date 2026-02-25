"""Yandex SpeechKit TTS"""
import aiohttp
from .base import BaseTTS


class YandexTTS(BaseTTS):
    URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

    def __init__(self, api_key: str, folder_id: str, voice: str = "alena",
                 emotion: str = "neutral", language: str = "ru-RU", sample_rate: int = 48000):
        self.api_key = api_key
        self.folder_id = folder_id
        self.voice = voice
        self.emotion = emotion
        self.language = language
        self.sample_rate = sample_rate
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def synthesize(self, text: str) -> dict:
        session = await self._get_session()
        headers = {"Authorization": f"Api-Key {self.api_key}"}
        data = {
            "text": text,
            "lang": self.language,
            "voice": self.voice,
            "emotion": self.emotion,
            "folderId": self.folder_id,
            "format": "lpcm",
            "sampleRateHertz": str(self.sample_rate),
        }
        async with session.post(
            self.URL, headers=headers, data=data,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 200:
                audio = await resp.read()
                return {
                    "audio": audio,
                    "sample_rate": self.sample_rate,
                    "format": "pcm16",
                }
            else:
                error = await resp.text()
                raise RuntimeError(f"TTS error {resp.status}: {error[:200]}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
