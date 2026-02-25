"""
Voice Activity Detection (VAD) — энергетический детектор речи.
Работает покадрово: каждый вызов feed() обрабатывает один аудио-чанк.
"""
import logging
from .audio import compute_rms

logger = logging.getLogger("core.vad")


class EnergyVAD:
    """
    Состояния:
      IDLE → речь не обнаружена
      SPEAKING → идёт речь, буфферизация
      SPEECH_END → тишина после речи, аудио готово

    Параметры:
      energy_threshold — порог RMS для детекции речи
      silence_frames — сколько фреймов тишины для конца речи
      min_speech_frames — сколько фреймов речи для начала детекции
    """

    def __init__(self, energy_threshold: int = 200,
                 silence_frames: int = 25,
                 min_speech_frames: int = 5,
                 enabled: bool = True):
        self.energy_threshold = energy_threshold
        self.silence_frames = silence_frames
        self.min_speech_frames = min_speech_frames
        self.enabled = enabled

        self.is_speaking = False
        self.speech_count = 0
        self.silence_count = 0
        self.audio_buffer = bytearray()

    def reset(self):
        self.is_speaking = False
        self.speech_count = 0
        self.silence_count = 0
        self.audio_buffer = bytearray()

    def feed(self, pcm_chunk: bytes) -> tuple[str, bytes | None]:
        """
        Обработать один аудио-чанк.

        Returns:
            ("speech_start", None) — начало речи
            ("speech_end", audio_bytes) — конец речи, накопленное аудио
            ("speaking", None) — речь продолжается
            ("silence", None) — тишина
            ("barge_in", None) — перебивание обнаружено (используется снаружи)
        """
        rms = compute_rms(pcm_chunk)

        if rms > self.energy_threshold:
            self.speech_count += 1
            self.silence_count = 0

            if not self.is_speaking and self.speech_count >= self.min_speech_frames:
                self.is_speaking = True
                self.audio_buffer = bytearray()
                self.audio_buffer.extend(pcm_chunk)
                return "speech_start", None

            if self.is_speaking:
                self.audio_buffer.extend(pcm_chunk)
                return "speaking", None

            return "silence", None
        else:
            if self.is_speaking:
                self.silence_count += 1
                self.audio_buffer.extend(pcm_chunk)

                if self.silence_count >= self.silence_frames:
                    audio = bytes(self.audio_buffer)
                    self.reset()
                    return "speech_end", audio

                return "speaking", None
            else:
                self.speech_count = 0
                return "silence", None

    def check_barge_in(self, pcm_chunk: bytes) -> bool:
        """Проверить, есть ли перебивание во время воспроизведения."""
        if not self.enabled:
            return False
        rms = compute_rms(pcm_chunk)
        if rms > self.energy_threshold:
            self.speech_count += 1
            if self.speech_count >= self.min_speech_frames:
                return True
        else:
            self.speech_count = 0
        return False

    def start_listening_after_barge_in(self, pcm_chunk: bytes):
        """Начать слушать после перебивания — сразу в режиме речи."""
        self.is_speaking = True
        self.audio_buffer = bytearray(pcm_chunk)
        self.silence_count = 0
        self.speech_count = 0
