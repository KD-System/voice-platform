"""
Аудио-утилиты: RMS, downsample, загрузка WAV.
Все функции работают с PCM16 (signed 16-bit little-endian).
"""
import struct
import wave
import logging

logger = logging.getLogger("core.audio")


def compute_rms(pcm_data: bytes) -> float:
    """Вычислить RMS энергию PCM16 фрагмента."""
    if len(pcm_data) < 2:
        return 0.0
    n = len(pcm_data) // 2
    samples = struct.unpack(f'<{n}h', pcm_data[:n * 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def downsample(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Даунсэмпл PCM16 с усреднением."""
    if from_rate == to_rate:
        return data
    ratio = from_rate // to_rate
    if ratio < 1:
        return data
    n = len(data) // 2
    samples = struct.unpack(f'<{n}h', data[:n * 2])
    out = []
    for i in range(0, n - ratio + 1, ratio):
        avg = sum(samples[i:i + ratio]) // ratio
        out.append(max(-32768, min(32767, avg)))
    return struct.pack(f'<{len(out)}h', *out)


def load_wav(path: str) -> tuple[bytes, int]:
    """
    Загрузить WAV файл.
    Returns: (pcm_data, sample_rate)
    """
    with wave.open(path, 'rb') as wf:
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    logger.info(f"WAV loaded: {path} ({len(pcm)} bytes @ {rate}Hz)")
    return pcm, rate


def save_wav(path: str, pcm_data: bytes, sample_rate: int = 8000):
    """Сохранить PCM16 в WAV."""
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
