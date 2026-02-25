"""ElevenLabs TTS — низкая latency, мультиязычный, с поддержкой SOCKS5"""
import io
import logging
import struct
import time
import aiohttp
try:
    from aiohttp_socks import ProxyConnector
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False
from .base import BaseTTS

logger = logging.getLogger("tts.elevenlabs")


class ElevenLabsTTS(BaseTTS):
    # Streaming endpoint для минимальной latency
    BASE_URL = "https://api.elevenlabs.io/v1/text-to-speech"

    def __init__(self, api_key: str, voice_id: str = "jAAHNNqlbAX9iWjJPEtE",
                 model_id: str = "eleven_multilingual_v2",
                 stability: float = 0.5, similarity_boost: float = 0.75,
                 style: float = 0.0, speed: float = 1.0,
                 sample_rate: int = 8000, proxy: str = "", **kwargs):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.style = style
        self.speed = speed
        self.sample_rate = sample_rate
        self.proxy = proxy
        self.session = None

        # ElevenLabs поддерживает PCM напрямую — без конвертации!
        self.output_format = "pcm_16000"
        self.pcm_rate = 16000

    async def _get_session(self):
        if self.session is None or self.session.closed:
            connector = None
            if self.proxy and HAS_SOCKS:
                connector = ProxyConnector.from_url(self.proxy)
                logger.info(f"ElevenLabs using proxy: {self.proxy}")
            elif self.proxy and not HAS_SOCKS:
                logger.warning("aiohttp-socks not installed, proxy ignored")
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    async def synthesize(self, text: str) -> dict:
        """
        Синтез речи через ElevenLabs API.
        Возвращает PCM int16 @ 16000Hz
        """
        if not text or not text.strip():
            return {"audio": b"", "sample_rate": self.pcm_rate}

        session = await self._get_session()
        t0 = time.time()

        url = f"{self.BASE_URL}/{self.voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/pcm",
        }
        payload = {
            "text": text.strip(),
            "model_id": self.model_id,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
                "style": self.style,
                "use_speaker_boost": True,
            },
        }

        # Параметры в URL
        params = {
            "output_format": self.output_format,
            "optimize_streaming_latency": "3",  # 0-4, higher = lower latency
        }

        try:
            async with session.post(
                url, headers=headers, json=payload, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"ElevenLabs error {resp.status}: {error[:200]}")
                    return {"audio": b"", "sample_rate": self.pcm_rate}

                # Читаем PCM данные
                audio_data = await resp.read()

                elapsed = time.time() - t0
                duration_s = len(audio_data) / (self.pcm_rate * 2)
                logger.info(f"ElevenLabs: {len(text)} chars → {len(audio_data)} bytes "
                           f"({duration_s:.1f}s audio) in {elapsed*1000:.0f}ms")

                return {
                    "audio": audio_data,
                    "sample_rate": self.pcm_rate,
                }

        except Exception as e:
            logger.error(f"ElevenLabs TTS error: {e}")
            return {"audio": b"", "sample_rate": self.pcm_rate}

    async def synthesize_stream(self, text: str):
        """
        Streaming синтез — возвращает chunks по мере генерации.
        Yields: bytes (PCM int16 chunks)
        """
        if not text or not text.strip():
            return

        session = await self._get_session()

        url = f"{self.BASE_URL}/{self.voice_id}/stream"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text.strip(),
            "model_id": self.model_id,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
                "style": self.style,
            },
        }
        params = {
            "output_format": self.output_format,
            "optimize_streaming_latency": "3",
        }

        try:
            async with session.post(
                url, headers=headers, json=payload, params=params,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"ElevenLabs stream error {resp.status}: {error[:200]}")
                    return

                async for chunk in resp.content.iter_any():
                    if chunk:
                        yield chunk

        except Exception as e:
            logger.error(f"ElevenLabs stream error: {e}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
