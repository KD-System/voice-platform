"""
Единая сессия голосового пайплайна.
FreeSWITCH → WebSocket → VAD → ASR → LLM(stream) → TTS → FreeSWITCH

Один класс обрабатывает любого робота — вся специфика в config.
"""
import asyncio
import logging
import time
from datetime import datetime

from asr import get_asr
from llm import get_llm
from tts import get_tts
from ..audio import downsample, load_wav
from ..vad import EnergyVAD
from ..playback import FSPlayback
from ..logging import send_telegram, format_call_report
from ..logging import save_call_log

logger = logging.getLogger("core.sessions.session_pipeline")


class PipelineSession:
    """
    Единая сессия звонка.
    Создаётся для каждого входящего WebSocket-соединения от FreeSWITCH.
    """

    def __init__(self, ws, call_id: str, cfg: dict, storage=None):
        self.ws = ws
        self.call_id = call_id
        self.cfg = cfg
        self.storage = storage  # db.Storage (опционально)

        # Состояние
        self.uuid = None
        self.is_active = True
        self.started_at = datetime.now()
        self.greeting_done = False
        self.barge_in_triggered = False

        # Контекст диалога
        self.messages = [{"role": "system", "content": cfg["system_prompt"]}]
        self.transcript = []
        self.turn_metrics = []
        self.total_turns = 0
        self.barge_in_count = 0
        self.caller_number = "unknown"

        # Провайдеры (создаются в start)
        self.asr_engine = None
        self.llm_engine = None
        self.tts_engine = None

        # VAD
        vad_cfg = cfg["vad"]
        self.vad = EnergyVAD(
            energy_threshold=vad_cfg["energy_threshold"],
            silence_frames=vad_cfg["silence_frames"],
            min_speech_frames=vad_cfg["min_speech_frames"],
            enabled=vad_cfg["enabled"],
        )

        # Playback (создаётся когда получим UUID)
        self.playback = None

        # Greeting PCM (предзагружен)
        self._greeting_pcm = None
        self._greeting_rate = 8000
        if cfg.get("greeting_wav"):
            try:
                self._greeting_pcm, self._greeting_rate = load_wav(cfg["greeting_wav"])
            except Exception as e:
                logger.warning(f"[{call_id}] Failed to load greeting.wav: {e}")

    async def start(self):
        """Инициализация провайдеров и приветствие."""
        secrets = self.cfg["secrets"]

        # === ASR ===
        asr_cfg = self.cfg["asr"]
        if asr_cfg["provider"] == "triton_armenian":
            self.asr_engine = get_asr(asr_cfg["provider"],
                                      server_url=asr_cfg.get("server_url", ""),
                                      model_name=asr_cfg.get("model_name", "streaming_asr"))
        else:
            self.asr_engine = get_asr(asr_cfg["provider"],
                                      api_key=secrets["yandex_api_key"],
                                      folder_id=secrets["yandex_folder_id"],
                                      language=asr_cfg["language"])

        # === TTS ===
        tts_cfg = self.cfg["tts"]
        if tts_cfg["provider"] == "zvukogram":
            self.tts_engine = get_tts("zvukogram",
                                      token=secrets["tts_token"],
                                      email=secrets["tts_email"],
                                      voice=tts_cfg.get("voice", "Ada AM"),
                                      speed=tts_cfg.get("speed", 1.0),
                                      pitch=tts_cfg.get("pitch", 0),
                                      sample_rate=tts_cfg.get("sample_rate", 8000))
        elif tts_cfg["provider"] == "elevenlabs":
            self.tts_engine = get_tts("elevenlabs",
                                      api_key=secrets["tts_api_key"],
                                      voice_id=tts_cfg.get("voice_id", ""),
                                      model_id=tts_cfg.get("model_id", "eleven_multilingual_v2"),
                                      stability=tts_cfg.get("stability", 0.5),
                                      similarity_boost=tts_cfg.get("similarity_boost", 0.75),
                                      speed=tts_cfg.get("speed", 1.0),
                                      proxy=tts_cfg.get("proxy", ""))
        else:
            self.tts_engine = get_tts("yandex",
                                      api_key=secrets["tts_api_key"] or secrets["yandex_api_key"],
                                      folder_id=secrets["yandex_folder_id"],
                                      voice=tts_cfg.get("voice", "alena"),
                                      language=tts_cfg.get("language", "ru-RU"))

        # === LLM ===
        llm_cfg = self.cfg["llm"]
        self.llm_engine = get_llm(llm_cfg.get("provider", "yandex"),
                                  api_key=secrets["yandex_api_key"],
                                  folder_id=secrets["yandex_folder_id"],
                                  model=llm_cfg.get("model", ""),
                                  temperature=llm_cfg.get("temperature", 0.5),
                                  max_tokens=llm_cfg.get("max_tokens", 80))

        # === Playback ===
        self.playback = FSPlayback(self.uuid, self.call_id)

        logger.info(f"[{self.call_id}] Providers ready: "
                     f"ASR={asr_cfg['provider']} TTS={tts_cfg['provider']} "
                     f"LLM={llm_cfg.get('provider', 'yandex')}")

        # Номер звонящего
        self.caller_number = await self.playback.get_caller_number()
        if self.caller_number != "unknown":
            logger.info(f"[{self.call_id}] Caller: {self.caller_number}")

        # === Storage: начало звонка ===
        if self.storage:
            try:
                from pathlib import Path
                robot_name = Path(self.cfg["robot_dir"]).name
                await self.storage.on_call_start(
                    call_id=self.call_id,
                    uuid=self.uuid or "",
                    caller=self.caller_number,
                    mode="pipeline",
                    robot_name=robot_name,
                    language=self.cfg["asr"].get("language", "ru-RU")[:2],
                )
            except Exception as e:
                logger.error(f"[{self.call_id}] Storage on_call_start: {e}")

        # === GREETING ===
        t0 = time.time()
        greeting_text = self.cfg.get("greeting_text", "")

        if self._greeting_pcm:
            logger.info(f"[{self.call_id}] Playing pre-recorded greeting")
            await self.playback.play_pcm(self._greeting_pcm, self._greeting_rate)
            if greeting_text:
                self.messages.append({"role": "assistant", "content": greeting_text})
                self.transcript.append(f"\U0001f916Bot: {greeting_text}")
        elif greeting_text:
            logger.info(f"[{self.call_id}] Greeting via TTS")
            self.messages.append({"role": "assistant", "content": greeting_text})
            self.transcript.append(f"\U0001f916Bot: {greeting_text}")
            await self._speak_text(greeting_text)

        logger.info(f"[{self.call_id}] Greeting done ({time.time() - t0:.2f}s), listening...")
        self.greeting_done = True

    async def stop(self):
        """Завершение сессии: логирование, telegram, cleanup."""
        self.is_active = False
        if self.playback:
            self.playback.close()

        dur = (datetime.now() - self.started_at).total_seconds()
        logger.info(f"[{self.call_id}] Call ended: {dur:.1f}s, "
                     f"{self.total_turns} turns, ctx={len(self.messages)}")

        # === Telegram ===
        secrets = self.cfg["secrets"]
        if self.transcript and self.cfg["telegram"].get("enabled"):
            asr_times = [m["asr_ms"] for m in self.turn_metrics]
            asr_avg = int(sum(asr_times) / len(asr_times)) if asr_times else 0
            report = format_call_report(
                caller=self.caller_number,
                uuid=self.uuid or "unknown",
                call_time=self.started_at.strftime("%d-%m-%Y %H:%M:%S"),
                duration=dur,
                turns=self.total_turns,
                barge_ins=self.barge_in_count,
                asr_avg_ms=asr_avg,
                transcript=self.transcript,
            )
            await send_telegram(secrets["tg_token"], secrets["tg_chat_id"], report)
            logger.info(f"[{self.call_id}] Telegram sent")

        # === JSON log ===
        save_call_log(
            robot_dir=self.cfg["robot_dir"],
            uuid=self.uuid or "unknown",
            caller=self.caller_number,
            call_time=self.started_at.strftime("%Y%m%d_%H%M%S"),
            duration=dur,
            turns=self.total_turns,
            barge_ins=self.barge_in_count,
            turn_metrics=self.turn_metrics,
            transcript=self.transcript,
        )

        # === Storage: завершение звонка ===
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

        # === Cleanup провайдеров ===
        for engine in [self.asr_engine, self.tts_engine, self.llm_engine]:
            if engine:
                try:
                    await engine.close()
                except Exception:
                    pass

    # ── VAD + обработка аудио ──────────────────────────────────────

    async def handle_audio(self, pcm_data: bytes):
        """Обработка каждого аудио-чанка от FreeSWITCH."""
        if not self.greeting_done:
            return

        # Во время воспроизведения — проверяем barge-in
        if self.playback and self.playback.is_playing:
            if self.vad.enabled and self.vad.check_barge_in(pcm_data):
                logger.info(f"[{self.call_id}] BARGE-IN")
                self.barge_in_count += 1
                self.barge_in_triggered = True
                await self.playback.stop()
                self.vad.start_listening_after_barge_in(pcm_data)
                if self.storage:
                    asyncio.create_task(self.storage.on_barge_in(call_id=self.call_id))
            return

        # Обычный режим — VAD
        event, audio = self.vad.feed(pcm_data)

        if event == "speech_start":
            logger.info(f"[{self.call_id}] >>> Speech")
        elif event == "speech_end":
            logger.info(f"[{self.call_id}] <<< End ({len(audio)} bytes)")
            asyncio.create_task(self._process_speech(audio))

    # ── PIPELINE: ASR → LLM(stream) → TTS ────────────────────────

    async def _process_speech(self, audio_data: bytes):
        """Полный пайплайн: распознавание → генерация → синтез."""
        t0 = time.time()
        self.total_turns += 1
        self.barge_in_triggered = False

        # === ASR ===
        ta = time.time()
        try:
            fs_rate = self.cfg.get("fs_sample_rate", 8000)
            r = await self.asr_engine.recognize(audio_data, sample_rate=fs_rate)
            text = r.get("text", "")
        except Exception as e:
            logger.error(f"[{self.call_id}] ASR err: {e}")
            return
        asr_ms = int((time.time() - ta) * 1000)

        if not text:
            logger.warning(f"[{self.call_id}] ASR empty ({asr_ms}ms)")
            return

        logger.info(f"[{self.call_id}] ASR ({asr_ms}ms): \"{text}\"")
        self.messages.append({"role": "user", "content": text})
        self.transcript.append(f"\U0001f9d1Client: {text}")
        self.turn_metrics.append({"turn": self.total_turns, "asr_ms": asr_ms, "text": text})

        # Storage: реплика пользователя
        if self.storage:
            asyncio.create_task(self.storage.on_user_speech(
                call_id=self.call_id, text=text,
                asr_provider=self.cfg["asr"]["provider"],
                asr_latency_ms=asr_ms,
            ))

        # === LLM → TTS streaming по предложениям ===
        tl = time.time()
        full_response = ""
        sentence_num = 0
        first_audio_ms = None

        try:
            async for sentence in self.llm_engine.chat_stream_sentences(self.messages):
                if not self.is_active or self.barge_in_triggered:
                    logger.info(f"[{self.call_id}] Stream interrupted")
                    break

                full_response += (" " if full_response else "") + sentence
                sentence_num += 1
                llm_ms = int((time.time() - tl) * 1000)

                if sentence_num == 1:
                    logger.info(f"[{self.call_id}] LLM 1st ({llm_ms}ms): \"{sentence[:60]}\"")
                else:
                    logger.info(f"[{self.call_id}] LLM #{sentence_num}: \"{sentence[:60]}\"")

                # TTS
                tt = time.time()
                try:
                    tr = await self.tts_engine.synthesize(sentence)
                    audio = tr.get("audio", b"")
                    rate = tr.get("sample_rate", 48000)
                    tts_ms = int((time.time() - tt) * 1000)
                    logger.info(f"[{self.call_id}] TTS ({tts_ms}ms): {len(audio)}b")

                    if first_audio_ms is None:
                        first_audio_ms = int((time.time() - t0) * 1000)
                        logger.info(f"[{self.call_id}] === FIRST AUDIO at {first_audio_ms}ms ===")

                    if audio and self.is_active and not self.barge_in_triggered:
                        await self.playback.play_pcm(audio, rate)
                except Exception as e:
                    logger.error(f"[{self.call_id}] TTS err: {e}")

        except Exception as e:
            logger.error(f"[{self.call_id}] LLM err: {e}")
            return

        if full_response.strip():
            self.messages.append({"role": "assistant", "content": full_response.strip()})
            self.transcript.append(f"\U0001f916Bot: {full_response.strip()}")

            # Storage: ответ бота
            if self.storage:
                llm_total_ms = int((time.time() - tl) * 1000)
                asyncio.create_task(self.storage.on_bot_response(
                    call_id=self.call_id,
                    text=full_response.strip(),
                    llm_provider=self.cfg["llm"].get("provider", "yandex"),
                    llm_latency_ms=llm_total_ms,
                    tts_provider=self.cfg["tts"]["provider"],
                    tts_latency_ms=0,
                ))

        total_ms = int((time.time() - t0) * 1000)
        logger.info(f"[{self.call_id}] Pipeline: {total_ms}ms total, ASR={asr_ms}ms, "
                     f"1st_audio={first_audio_ms}ms, sentences={sentence_num}, "
                     f"ctx={len(self.messages)}")

    # ── Вспомогательные ──────────────────────────────────────────

    async def _speak_text(self, text: str):
        """Синтезировать текст и проиграть."""
        try:
            r = await self.tts_engine.synthesize(text)
            audio = r.get("audio", b"")
            rate = r.get("sample_rate", 48000)
            if audio:
                await self.playback.play_pcm(audio, rate)
        except Exception as e:
            logger.error(f"[{self.call_id}] speak err: {e}")
