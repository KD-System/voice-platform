"""
Воспроизведение аудио через FreeSWITCH.
uuid_broadcast — проиграть WAV
uuid_break — остановить (barge-in)
"""
import asyncio
import os
import logging
from .audio import downsample, save_wav

logger = logging.getLogger("core.playback")

TTS_DIR = "/tmp/voice_pipeline"
os.makedirs(TTS_DIR, exist_ok=True)


class FSPlayback:
    """Управление воспроизведением через FreeSWITCH CLI."""

    def __init__(self, uuid: str, call_id: str):
        self.uuid = uuid
        self.call_id = call_id
        self.is_playing = False
        self.is_active = True
        self._file_counter = 0

    async def play_pcm(self, pcm_data: bytes, sample_rate: int = 8000) -> bool:
        """
        Проиграть PCM-аудио в канал FreeSWITCH.
        Автоматически даунсэмплит до 8kHz.
        Возвращает True если воспроизведение завершилось нормально.
        """
        if not pcm_data or not self.uuid:
            return False

        # Даунсэмпл до 8kHz для FreeSWITCH
        if sample_rate != 8000:
            pcm_data = downsample(pcm_data, sample_rate, 8000)

        idx = self._file_counter
        self._file_counter += 1
        wav_path = f"{TTS_DIR}/{self.call_id}_{idx}.wav"

        save_wav(wav_path, pcm_data, 8000)

        duration_ms = len(pcm_data) // 16  # PCM16 @ 8kHz: 16 bytes/ms
        self.is_playing = True

        try:
            proc = await asyncio.create_subprocess_exec(
                "fs_cli", "-x", f"uuid_broadcast {self.uuid} {wav_path} aleg",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()

            if "+OK" not in stdout.decode():
                self.is_playing = False
                return False

            # Ждём пока закончится проигрывание (или barge-in)
            elapsed = 0
            while elapsed < duration_ms and self.is_playing and self.is_active:
                await asyncio.sleep(0.05)
                elapsed += 50

            return self.is_playing  # True = нормально, False = прервано
        except Exception as e:
            logger.error(f"[{self.call_id}] play err: {e}")
            return False
        finally:
            self.is_playing = False
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    async def stop(self):
        """Остановить воспроизведение (barge-in)."""
        if self.uuid and self.is_playing:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "fs_cli", "-x", f"uuid_break {self.uuid} all",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
                self.is_playing = False
                logger.info(f"[{self.call_id}] Playback stopped (barge-in)")
            except Exception as e:
                logger.error(f"[{self.call_id}] stop err: {e}")

    async def get_caller_number(self) -> str:
        """Получить номер звонящего через fs_cli."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "fs_cli", "-x", f"uuid_getvar {self.uuid} caller_id_number",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            num = stdout.decode().strip()
            if num and "-ERR" not in num:
                return num
        except Exception:
            pass
        return "unknown"

    def close(self):
        self.is_active = False
