"""Yandex SpeechKit TTS (v1 + v3 Brand Voice)"""
import aiohttp
from .base import BaseTTS


class YandexTTS(BaseTTS):
    URL_V1 = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
    URL_V3 = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"

    def __init__(self, api_key: str, folder_id: str, voice: str = "",
                 emotion: str = "neutral", language: str = "ru-RU",
                 sample_rate: int = 48000, model_uri: str = "",
                 speed: float = 1.0, role: str = ""):
        self.api_key = api_key
        self.folder_id = folder_id
        self.voice = voice
        self.emotion = emotion
        self.language = language
        self.sample_rate = sample_rate
        self.model_uri = model_uri
        self.speed = speed
        self.role = role
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def synthesize(self, text: str) -> dict:
        if self.model_uri:
            return await self._synthesize_v3(text)
        return await self._synthesize_v1(text)

    async def _synthesize_v1(self, text: str) -> dict:
        """Standard Yandex TTS v1 API."""
        session = await self._get_session()
        headers = {"Authorization": f"Api-Key {self.api_key}"}
        data = {
            "text": text,
            "lang": self.language,
            "voice": self.voice or "alena",
            "emotion": self.emotion,
            "folderId": self.folder_id,
            "format": "lpcm",
            "sampleRateHertz": str(self.sample_rate),
        }
        async with session.post(
            self.URL_V1, headers=headers, data=data,
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
                raise RuntimeError(f"TTS v1 error {resp.status}: {error[:200]}")

    async def _synthesize_v3(self, text: str) -> dict:
        """Yandex TTS v3 API for Brand Voice."""
        session = await self._get_session()
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        hints = []
        if self.speed and self.speed != 1.0:
            hints.append({"speed": self.speed})
        if self.role:
            hints.append({"role": self.role})

        body = {
            "text": text,
            "model": self.model_uri,
            "outputAudioSpec": {
                "rawAudio": {
                    "audioEncoding": "LINEAR16_PCM",
                    "sampleRateHertz": self.sample_rate,
                }
            },
            "hints": hints,
            "loudnessNormalizationType": "LUFS",
        }
        async with session.post(
            self.URL_V3, headers=headers, json=body,
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
                raise RuntimeError(f"TTS v3 error {resp.status}: {error[:200]}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
