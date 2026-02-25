"""
Единый загрузчик конфигурации.
Приоритет: config.json робота → .env робота → .env корня платформы → дефолты
"""
import json
import os
from pathlib import Path
from dotenv import load_dotenv


# Дефолтная конфигурация
DEFAULTS = {
    "ws_host": "0.0.0.0",
    "ws_port": 5200,
    "fs_sample_rate": 8000,
    "mode": "pipeline",

    "asr": {
        "provider": "yandex",
        "language": "ru-RU",
        "server_url": "",
        "model_name": "streaming_asr",
    },
    "llm": {
        "provider": "yandex",
        "temperature": 0.5,
        "max_tokens": 80,
    },
    "tts": {
        "provider": "yandex",
        "voice": "alena",
        "language": "ru-RU",
        "speed": 1.0,
        "pitch": 0,
        "sample_rate": 48000,
        "voice_id": "",
        "model_id": "eleven_multilingual_v2",
        "stability": 0.5,
        "similarity_boost": 0.75,
        "proxy": "",
    },
    "vad": {
        "enabled": True,
        "energy_threshold": 200,
        "silence_frames": 25,
        "min_speech_frames": 5,
    },
    "telegram": {
        "enabled": True,
    },
    "greeting_text": "",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивное слияние: override перезаписывает base."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(robot_dir: str | Path) -> dict:
    """
    Загружает полную конфигурацию для робота.

    1. Базовые дефолты
    2. config.json из папки робота
    3. .env из папки робота → .env из корня платформы
    4. Секреты (API keys) из .env
    """
    robot_dir = Path(robot_dir).resolve()
    platform_root = robot_dir
    # Ищем корень платформы (где лежит core/)
    for _ in range(5):
        if (platform_root / "core").is_dir():
            break
        platform_root = platform_root.parent

    # Загружаем .env (робот → корень)
    robot_env = robot_dir / ".env"
    root_env = platform_root / ".env"
    if robot_env.exists():
        load_dotenv(robot_env, override=True)
    if root_env.exists():
        load_dotenv(root_env, override=False)

    # Загружаем config.json
    config_file = robot_dir / "config.json"
    file_config = {}
    if config_file.exists():
        file_config = json.loads(config_file.read_text(encoding="utf-8"))

    # Обратная совместимость: interruption/config.json → vad
    interruption_file = robot_dir / "interruption" / "config.json"
    if interruption_file.exists() and "vad" not in file_config:
        intr = json.loads(interruption_file.read_text(encoding="utf-8"))
        file_config["vad"] = {
            "enabled": intr.get("enabled", True),
            "energy_threshold": intr.get("vad_energy_threshold", 200),
            "silence_frames": intr.get("vad_silence_frames", 25),
            "min_speech_frames": intr.get("vad_min_speech_frames", 5),
        }

    # Слияние: defaults + config.json
    cfg = _deep_merge(DEFAULTS, file_config)

    # Секреты из .env (не хранятся в config.json)
    cfg["secrets"] = {
        "yandex_api_key": os.getenv("YANDEX_API_KEY", ""),
        "yandex_folder_id": os.getenv("YANDEX_FOLDER_ID", ""),
        "tts_api_key": os.getenv("TTS_API_KEY", os.getenv("YANDEX_API_KEY", "")),
        "tts_token": os.getenv("TTS_TOKEN", ""),
        "tts_email": os.getenv("TTS_EMAIL", ""),
        "tg_token": os.getenv("TG_TOKEN", ""),
        "tg_chat_id": os.getenv("TG_CHAT_ID", ""),
        "yandex_realtime_url": os.getenv("YANDEX_REALTIME_URL", ""),
    }

    # LLM model: если не задана — строим из folder_id
    if not cfg["llm"].get("model"):
        folder_id = cfg["secrets"]["yandex_folder_id"]
        cfg["llm"]["model"] = f"gpt://{folder_id}/yandexgpt/rc"

    # prompt.txt
    prompt_file = robot_dir / "prompt.txt"
    cfg["system_prompt"] = prompt_file.read_text(encoding="utf-8").strip() if prompt_file.exists() else "You are a helpful voice assistant."

    # greeting.wav
    greeting_wav = robot_dir / "greeting.wav"
    cfg["greeting_wav"] = str(greeting_wav) if greeting_wav.exists() else ""

    # Мета
    cfg["robot_dir"] = str(robot_dir)
    cfg["platform_root"] = str(platform_root)

    return cfg
