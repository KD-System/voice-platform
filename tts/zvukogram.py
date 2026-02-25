"""
Zvukogram TTS — адаптер для платформы
API: https://zvukogram.com/index.php?r=api/text
Возвращает PCM audio для телефонии
"""
import io
import logging
import struct
import wave

import aiohttp

from .base import BaseTTS

logger = logging.getLogger(__name__)


class ZvukogramTTS(BaseTTS):
    URL = "https://zvukogram.com/index.php?r=api/text"

    def __init__(self, token: str, email: str, voice: str = "Ada AM",
                 speed: float = 1.0, pitch: int = 0, sample_rate: int = 8000, **kwargs):
        self.token = token
        self.email = email
        self.voice = voice
        self.speed = speed
        self.pitch = pitch
        self.sample_rate = sample_rate
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def synthesize(self, text: str) -> dict:
        """
        Синтезировать речь через Zvukogram API.

        Для текстов > 1000 символов — разбиваем на части.

        Returns:
            {"audio": pcm_bytes, "sample_rate": 8000, "format": "pcm16"}
        """
        session = await self._get_session()

        # Разбиваем длинный текст на части до 1000 символов
        chunks = self._split_text(text, max_len=900)
        all_pcm = bytearray()

        for chunk in chunks:
            pcm = await self._synthesize_chunk(session, chunk)
            if pcm:
                all_pcm.extend(pcm)

        if not all_pcm:
            raise RuntimeError("TTS returned no audio")

        logger.info(f"Zvukogram TTS: {len(text)} chars → {len(all_pcm)} bytes PCM @ {self.sample_rate}Hz")

        return {
            "audio": bytes(all_pcm),
            "sample_rate": self.sample_rate,
            "format": "pcm16",
        }

    async def _synthesize_chunk(self, session: aiohttp.ClientSession, text: str) -> bytes:
        """Синтезировать один чанк текста (до 1000 символов)"""
        data = {
            "token": self.token,
            "email": self.email,
            "voice": self.voice,
            "text": text,
            "format": "wav",
            "speed": str(self.speed),
            "pitch": str(self.pitch),
            "sample_rate": str(self.sample_rate),
            "channels": "1",
        }

        try:
            async with session.post(
                self.URL, data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"Zvukogram HTTP error {resp.status}: {error[:200]}")
                    return b""

                result = await resp.json()

                if result.get("status") != 1:
                    error = result.get("error", "Unknown error")
                    logger.error(f"Zvukogram API error: {error}")
                    return b""

                file_url = result.get("file", "")
                if not file_url:
                    logger.error("Zvukogram: no file URL in response")
                    return b""

                cost = result.get("cost", 0)
                balance = result.get("balans", "?")
                duration = result.get("duration", 0)
                logger.info(
                    f"Zvukogram: {duration}s audio, cost={cost} tokens, balance={balance}"
                )

                # Скачиваем WAV файл
                async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=15)) as audio_resp:
                    if audio_resp.status != 200:
                        logger.error(f"Zvukogram: failed to download audio: {audio_resp.status}")
                        return b""
                    wav_bytes = await audio_resp.read()

                # Извлекаем PCM из WAV
                pcm = self._wav_to_pcm(wav_bytes)
                return pcm

        except Exception as e:
            logger.error(f"Zvukogram TTS error: {e}")
            return b""

    @staticmethod
    def _wav_to_pcm(wav_bytes: bytes) -> bytes:
        """Извлечь сырые PCM данные из WAV файла"""
        try:
            with io.BytesIO(wav_bytes) as f:
                with wave.open(f, 'rb') as wf:
                    return wf.readframes(wf.getnframes())
        except Exception as e:
            logger.error(f"WAV parse error: {e}")
            return b""

    @staticmethod
    def _split_text(text: str, max_len: int = 900) -> list:
        """Разбить текст на части по предложениям, каждая до max_len символов"""
        if len(text) <= max_len:
            return [text]

        chunks = []
        current = ""

        # Разбиваем по предложениям
        sentences = []
        temp = ""
        for char in text:
            temp += char
            if char in '.!?։' and len(temp) > 1:
                sentences.append(temp.strip())
                temp = ""
        if temp.strip():
            sentences.append(temp.strip())

        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= max_len:
                current = f"{current} {sentence}".strip() if current else sentence
            else:
                if current:
                    chunks.append(current)
                current = sentence

        if current:
            chunks.append(current)

        return chunks if chunks else [text[:max_len]]

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
