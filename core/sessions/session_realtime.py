"""
Realtime-сессия — Yandex Realtime API.
FreeSWITCH ↔ WebSocket ↔ Yandex Realtime (full-duplex)

Отличие от pipeline:
- НЕТ отдельных ASR/LLM/TTS — всё делает Yandex Realtime API
- Server-side VAD (детекция речи на стороне Yandex)
- Двунаправленный аудиопоток через WebSocket
- Barge-in по событию speech_started от API
"""
import asyncio
import base64
import json
import logging
import os
from datetime import datetime

import websockets

from ..audio import downsample, save_wav
from ..logging import send_telegram, format_call_report
from ..logging import save_call_log

logger = logging.getLogger("core.sessions.session_realtime")

TTS_DIR = "/tmp/voice_pipeline"
os.makedirs(TTS_DIR, exist_ok=True)


class RealtimeSession:
    """
    Full-duplex сессия с Yandex Realtime API.
    FS аудио → base64 → Yandex WS
    Yandex WS → PCM → FS playback
    """

    def __init__(self, ws, call_id: str, cfg: dict, storage=None):
        self.fs_ws = ws
        self.call_id = call_id
        self.cfg = cfg
        self.storage = storage  # db.Storage (опционально)

        # Состояние
        self.uuid = None
        self.is_active = True
        self.started_at = datetime.now()
        self.caller_number = "unknown"

        # Yandex Realtime WebSocket
        self.ai_ws = None
        self.ai_ready = asyncio.Event()

        # Аудио-буфер ответа
        self.response_audio = bytearray()

        # Playback
        self.is_playing = False
        self._file_counter = 0
        self._temp_files = []

        # Метрики
        self.transcript = []
        self.total_turns = 0
        self.barge_in_count = 0
        self.audio_chunks_sent = 0

    async def start(self):
        """Подключиться к Yandex Realtime API."""
        secrets = self.cfg["secrets"]
        realtime_cfg = self.cfg.get("realtime", {})
        realtime_url = realtime_cfg.get("url", secrets.get("yandex_realtime_url", ""))

        if not realtime_url:
            logger.error(f"[{self.call_id}] No realtime URL configured")
            return

        headers = {"Authorization": f"api-key {secrets['yandex_api_key']}"}
        try:
            self.ai_ws = await websockets.connect(
                realtime_url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                max_size=None,
            )
            logger.info(f"[{self.call_id}] Connected to Yandex Realtime")

            # Настройка сессии
            voice = realtime_cfg.get("voice", "jane")
            vad_threshold = realtime_cfg.get("vad_threshold", 0.5)
            silence_ms = realtime_cfg.get("silence_duration_ms", 500)
            prefix_ms = realtime_cfg.get("prefix_padding_ms", 300)

            await self.ai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": self.cfg["system_prompt"],
                    "voice": voice,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "general"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": vad_threshold,
                        "prefix_padding_ms": prefix_ms,
                        "silence_duration_ms": silence_ms,
                    },
                },
            }))

            self.ai_ready.set()

            # Storage: начало звонка
            if self.storage:
                try:
                    from pathlib import Path
                    robot_name = Path(self.cfg["robot_dir"]).name
                    await self.storage.on_call_start(
                        call_id=self.call_id,
                        uuid=self.uuid or "",
                        caller=self.caller_number,
                        mode="realtime",
                        robot_name=robot_name,
                        language="ru",
                    )
                except Exception as e:
                    logger.error(f"[{self.call_id}] Storage on_call_start: {e}")

            # Приветствие — AI говорит первым
            await self.ai_ws.send(json.dumps({"type": "response.create"}))

            # Запускаем обработку ответов от AI
            asyncio.create_task(self._handle_ai_response())

        except Exception as e:
            logger.error(f"[{self.call_id}] AI connect failed: {e}")

    async def stop(self):
        """Завершение сессии."""
        self.is_active = False
        dur = (datetime.now() - self.started_at).total_seconds()
        logger.info(f"[{self.call_id}] Call ended: {dur:.1f}s, {self.total_turns} turns")

        # Telegram
        secrets = self.cfg["secrets"]
        if self.transcript and self.cfg["telegram"].get("enabled"):
            report = format_call_report(
                caller=self.caller_number,
                uuid=self.uuid or "unknown",
                call_time=self.started_at.strftime("%d-%m-%Y %H:%M:%S"),
                duration=dur,
                turns=self.total_turns,
                barge_ins=self.barge_in_count,
                asr_avg_ms=0,
                transcript=self.transcript,
            )
            await send_telegram(secrets["tg_token"], secrets["tg_chat_id"], report)

        # JSON log
        save_call_log(
            robot_dir=self.cfg["robot_dir"],
            uuid=self.uuid or "unknown",
            caller=self.caller_number,
            call_time=self.started_at.strftime("%Y%m%d_%H%M%S"),
            duration=dur,
            turns=self.total_turns,
            barge_ins=self.barge_in_count,
            turn_metrics=[],
            transcript=self.transcript,
        )

        # Storage: завершение звонка
        if self.storage:
            try:
                await self.storage.on_call_end(
                    call_id=self.call_id,
                    duration_sec=dur,
                    turns=self.total_turns,
                    barge_ins=self.barge_in_count,
                )
            except Exception as e:
                logger.error(f"[{self.call_id}] Storage on_call_end: {e}")

        # Cleanup
        if self.ai_ws:
            try:
                await self.ai_ws.close()
            except Exception:
                pass
        for f in self._temp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    async def handle_audio(self, pcm_data: bytes):
        """Отправить аудио-чанк от FS в Yandex Realtime."""
        if not self.ai_ready.is_set():
            try:
                await asyncio.wait_for(self.ai_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                return

        # Не отправляем аудио во время проигрывания (эхо)
        if self.is_playing:
            return

        self.audio_chunks_sent += 1
        try:
            await self.ai_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm_data).decode(),
            }))
        except Exception:
            pass

    # ── Обработка ответов от AI ─────────────────────────────────

    async def _handle_ai_response(self):
        """Читать события от Yandex Realtime API."""
        try:
            async for message in self.ai_ws:
                if not self.is_active:
                    break

                event = json.loads(message)
                t = event.get("type", "")

                if t == "response.output_audio.delta":
                    b64 = event.get("delta", "")
                    if b64:
                        self.response_audio.extend(base64.b64decode(b64))

                elif t == "response.output_text.delta":
                    d = event.get("delta", "")
                    if d:
                        logger.info(f"[{self.call_id}] AI: {d}")
                        self._last_response_text = getattr(self, '_last_response_text', '') + d

                elif t == "response.done":
                    buf_size = len(self.response_audio)
                    logger.info(f"[{self.call_id}] Response done ({buf_size}b)")
                    if self.response_audio:
                        await self._play_response(bytes(self.response_audio))
                        self.response_audio = bytearray()
                    self.total_turns += 1
                    # Storage: ответ бота
                    resp_text = getattr(self, '_last_response_text', '')
                    if resp_text and self.storage:
                        self.transcript.append(f"\U0001f916Bot: {resp_text}")
                        asyncio.create_task(self.storage.on_bot_response(
                            call_id=self.call_id, text=resp_text,
                            llm_provider="yandex_realtime",
                            llm_latency_ms=0,
                        ))
                    self._last_response_text = ''

                elif t == "conversation.item.input_audio_transcription.completed":
                    tr = event.get("transcript", "")
                    if tr:
                        logger.info(f"[{self.call_id}] User: {tr}")
                        self.transcript.append(f"\U0001f9d1Client: {tr}")
                        if self.storage:
                            asyncio.create_task(self.storage.on_user_speech(
                                call_id=self.call_id, text=tr,
                                asr_provider="yandex_realtime",
                                asr_latency_ms=0,
                            ))

                elif t == "input_audio_buffer.speech_started":
                    logger.info(f"[{self.call_id}] >>> Speech")
                    # Barge-in: очистить буфер и остановить проигрывание
                    self.response_audio = bytearray()
                    self.barge_in_count += 1
                    await self._stop_playback()
                    if self.storage:
                        asyncio.create_task(self.storage.on_barge_in(call_id=self.call_id))

                elif t == "input_audio_buffer.speech_stopped":
                    logger.info(f"[{self.call_id}] <<< Silence")

                elif t == "response.created":
                    logger.info(f"[{self.call_id}] Generating...")

                elif t == "error":
                    logger.error(f"[{self.call_id}] AI error: {event.get('error')}")

                elif t in ("session.created", "session.updated"):
                    logger.info(f"[{self.call_id}] {t}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"[{self.call_id}] AI closed: {e}")
        except Exception as e:
            logger.error(f"[{self.call_id}] AI error: {e}")
        finally:
            self.is_active = False

    # ── Playback ────────────────────────────────────────────────

    async def _play_response(self, audio_48k: bytes):
        """Даунсэмпл 48kHz→8kHz и проиграть через FS."""
        if not self.uuid or not self.is_active:
            return

        audio_8k = downsample(audio_48k, 48000, 8000)
        idx = self._file_counter
        self._file_counter += 1
        wav_path = f"{TTS_DIR}/{self.call_id}_{idx}.wav"
        self._temp_files.append(wav_path)

        save_wav(wav_path, audio_8k, 8000)

        duration_ms = len(audio_8k) // 16
        logger.info(f"[{self.call_id}] Playing {duration_ms}ms")

        self.is_playing = True
        try:
            proc = await asyncio.create_subprocess_exec(
                "fs_cli", "-x", f"uuid_broadcast {self.uuid} {wav_path} aleg",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()

            if "+OK" not in stdout.decode():
                self.is_playing = False
                return

            elapsed = 0
            while elapsed < duration_ms and self.is_playing and self.is_active:
                await asyncio.sleep(0.1)
                elapsed += 100
        except Exception as e:
            logger.error(f"[{self.call_id}] Play error: {e}")
        finally:
            self.is_playing = False

    async def _stop_playback(self):
        """Остановить проигрывание (barge-in)."""
        if self.uuid and self.is_playing:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "fs_cli", "-x", f"uuid_break {self.uuid} all",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
                self.is_playing = False
                logger.info(f"[{self.call_id}] Barge-in: playback stopped")
            except Exception:
                pass
