#!/usr/bin/env python3
"""
Voice Platform — единая точка входа.

Режимы (задаются в config.json → "mode"):
  pipeline    — VAD → ASR → LLM(stream) → TTS → FreeSWITCH
  realtime    — Yandex Realtime API (full-duplex, server VAD)
  llm_script  — VAD → ASR → LLM выбирает WAV → play

Запуск:
  python run.py robots/pipeline_russian
  python run.py robots/realtime_russian
  python run.py robots/llm_script_russian
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

import websockets

# Путь к корню платформы
PLATFORM_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PLATFORM_ROOT))

from core.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("run")

call_counter = 0

MODES = {
    "pipeline": "core.sessions.session_pipeline:PipelineSession",
    "realtime": "core.sessions.session_realtime:RealtimeSession",
    "llm_script": "core.sessions.session_llm_script:LLMScriptSession",
}


def _load_session_class(mode: str):
    """Загрузить класс сессии по имени режима."""
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}. Available: {list(MODES.keys())}")

    module_path, class_name = MODES[mode].rsplit(":", 1)
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


def make_connection_handler(cfg: dict, SessionClass):
    """Создаёт обработчик WebSocket-соединений с привязкой к конфигу."""

    async def handle_connection(websocket):
        global call_counter
        call_counter += 1
        call_id = f"call-{call_counter:04d}"

        session = SessionClass(websocket, call_id, cfg)
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    # Первое сообщение может содержать UUID в начале
                    if session.uuid is None and len(message) > 36:
                        try:
                            possible_uuid = message[:36].decode('ascii')
                            if '-' in possible_uuid:
                                session.uuid = possible_uuid
                                logger.info(f"[{call_id}] UUID: {session.uuid}")
                                asyncio.create_task(session.start())
                                continue
                        except (UnicodeDecodeError, ValueError):
                            pass
                    await session.handle_audio(message)

                elif isinstance(message, str):
                    # UUID может прийти как JSON или plain text
                    try:
                        data = json.loads(message)
                        if "uuid" in data:
                            session.uuid = data["uuid"]
                            logger.info(f"[{call_id}] UUID: {session.uuid}")
                            asyncio.create_task(session.start())
                    except json.JSONDecodeError:
                        if '-' in message and len(message) < 50:
                            session.uuid = message.strip()
                            logger.info(f"[{call_id}] UUID: {session.uuid}")
                            asyncio.create_task(session.start())

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"[{call_id}] Error: {e}")
        finally:
            await session.stop()

    return handle_connection


async def main(robot_dir: str):
    cfg = load_config(robot_dir)

    mode = cfg.get("mode", "pipeline")
    host = cfg.get("ws_host", "0.0.0.0")
    port = cfg.get("ws_port", 5200)

    SessionClass = _load_session_class(mode)

    # Лог конфигурации
    logger.info("=" * 50)
    logger.info(f"Voice Platform v3 — {Path(robot_dir).name}")
    logger.info(f"Mode: {mode}")

    if mode == "pipeline":
        asr_p = cfg["asr"]["provider"]
        tts_p = cfg["tts"]["provider"]
        llm_p = cfg["llm"].get("provider", "yandex")
        logger.info(f"ASR: {asr_p} | TTS: {tts_p} | LLM: {llm_p}")
    elif mode == "realtime":
        logger.info(f"Yandex Realtime API (full-duplex)")
    elif mode == "llm_script":
        asr_p = cfg["asr"]["provider"]
        llm_p = cfg["llm"].get("provider", "yandex")
        logger.info(f"ASR: {asr_p} | LLM: {llm_p} | LLM-Script mode")

    vad_on = cfg["vad"]["enabled"]
    has_greeting = bool(cfg.get("greeting_wav"))
    greeting_text = cfg.get("greeting_text", "")
    gm = "WAV" if has_greeting else ("TTS" if greeting_text else "NONE")
    logger.info(f"Greeting: {gm} | Barge-in: {'ON' if vad_on else 'OFF'}")
    logger.info(f"ws://{host}:{port}")
    logger.info("=" * 50)

    handler = make_connection_handler(cfg, SessionClass)
    async with websockets.serve(handler, host, port, max_size=None,
                                ping_interval=20, ping_timeout=10):
        await asyncio.Future()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run.py <robot_dir>")
        print()
        print("Modes (set in config.json):")
        print("  pipeline    — VAD → ASR → LLM → TTS")
        print("  realtime    — Yandex Realtime API")
        print("  llm_script  — VAD → ASR → LLM chooses WAV → play")
        print()
        print("Example: python run.py robots/pipeline_russian")
        sys.exit(1)

    robot_path = Path(sys.argv[1])
    if not robot_path.is_absolute():
        robot_path = PLATFORM_ROOT / robot_path

    if not robot_path.is_dir():
        print(f"Error: {robot_path} is not a directory")
        sys.exit(1)

    asyncio.run(main(str(robot_path)))
