"""
LLM-Script сессия — LLM выбирает какой WAV-файл проиграть.
FreeSWITCH → WebSocket → VAD → ASR → LLM → play WAV

Отличие от pipeline:
- НЕТ TTS — ответы заранее записаны как WAV-файлы
- LLM получает в системном промпте список доступных файлов
- LLM возвращает ТОЛЬКО имя файла — этот файл и проигрывается

Ключевая особенность:
- LLM решает какой трек играть на основе контекста диалога
- Есть контекст диалога — LLM видит историю
"""
import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from asr import get_asr
from llm import get_llm
from ..audio import load_wav
from ..vad import EnergyVAD
from ..playback import FSPlayback
from ..logging import send_telegram, format_call_report
from ..logging import save_call_log

logger = logging.getLogger("core.sessions.session_llm_script")


class LLMScriptSession:
    """
    LLM-Script сессия: приветствие → слушай → ASR → LLM выбирает WAV → play.
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
        self.messages = []
        self.transcript = []
        self.turn_metrics = []
        self.total_turns = 0
        self.barge_in_count = 0
        self.caller_number = "unknown"

        # Провайдеры
        self.asr_engine = None
        self.llm_engine = None

        # VAD
        vad_cfg = cfg["vad"]
        self.vad = EnergyVAD(
            energy_threshold=vad_cfg["energy_threshold"],
            silence_frames=vad_cfg["silence_frames"],
            min_speech_frames=vad_cfg["min_speech_frames"],
            enabled=vad_cfg["enabled"],
        )

        # Playback
        self.playback = None

        # WAV-файлы: имя → (pcm, rate)
        self._tracks = {}
        self._load_tracks()

        # Системный промпт с перечнем файлов
        track_names = sorted(self._tracks.keys())
        files_list = "\n".join(f"- {name}" for name in track_names)

        base_prompt = cfg.get("system_prompt", "")
        self._system_prompt = (
            f"{base_prompt}\n\n"
            f"ДОСТУПНЫЕ АУДИО-ФАЙЛЫ:\n{files_list}\n\n"
            f"ПРАВИЛО: На каждую реплику пользователя ты ОБЯЗАН ответить "
            f"РОВНО одним именем файла из списка выше. "
            f"Ничего кроме имени файла. Без кавычек, без пояснений, без точек. "
            f"Только имя файла, например: {track_names[0] if track_names else 'track_1.wav'}"
        )

        self.messages = [{"role": "system", "content": self._system_prompt}]

        # Greeting
        self._greeting_pcm = None
        self._greeting_rate = 8000
        if cfg.get("greeting_wav"):
            try:
                self._greeting_pcm, self._greeting_rate = load_wav(cfg["greeting_wav"])
            except Exception as e:
                logger.warning(f"[{call_id}] Failed to load greeting.wav: {e}")

    def _load_tracks(self):
        """Загрузить все WAV-файлы из папки tracks/ робота."""
        robot_dir = Path(self.cfg["robot_dir"])
        tracks_dir = robot_dir / "tracks"

        if not tracks_dir.exists():
            # Фолбэк: WAV-файлы прямо в папке робота (кроме greeting.wav)
            tracks_dir = robot_dir

        for wav_file in sorted(tracks_dir.glob("*.wav")):
            if wav_file.name == "greeting.wav":
                continue
            try:
                pcm, rate = load_wav(str(wav_file))
                self._tracks[wav_file.name] = (pcm, rate)
            except Exception as e:
                logger.warning(f"[{self.call_id}] Failed to load {wav_file.name}: {e}")

        logger.info(f"[{self.call_id}] Loaded {len(self._tracks)} tracks: "
                     f"{list(self._tracks.keys())}")

    async def start(self):
        """Инициализация ASR, LLM и приветствие."""
        secrets = self.cfg["secrets"]

        # ASR
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

        # LLM
        llm_cfg = self.cfg["llm"]
        self.llm_engine = get_llm(llm_cfg.get("provider", "yandex"),
                                  api_key=secrets["yandex_api_key"],
                                  folder_id=secrets["yandex_folder_id"],
                                  model=llm_cfg.get("model", ""),
                                  temperature=llm_cfg.get("temperature", 0.3),
                                  max_tokens=llm_cfg.get("max_tokens", 50))

        # Playback
        self.playback = FSPlayback(self.uuid, self.call_id)

        logger.info(f"[{self.call_id}] LLM-Script mode: "
                     f"ASR={asr_cfg['provider']}, LLM={llm_cfg.get('provider', 'yandex')}, "
                     f"{len(self._tracks)} tracks")

        # Номер звонящего
        self.caller_number = await self.playback.get_caller_number()

        # Storage: начало звонка
        if self.storage:
            try:
                robot_name = Path(self.cfg["robot_dir"]).name
                await self.storage.on_call_start(
                    call_id=self.call_id,
                    uuid=self.uuid or "",
                    caller=self.caller_number,
                    mode="llm_script",
                    robot_name=robot_name,
                    language=self.cfg["asr"].get("language", "ru-RU")[:2],
                )
            except Exception as e:
                logger.error(f"[{self.call_id}] Storage on_call_start: {e}")

        # Greeting
        t0 = time.time()
        if self._greeting_pcm:
            logger.info(f"[{self.call_id}] Playing greeting")
            await self.playback.play_pcm(self._greeting_pcm, self._greeting_rate)

        logger.info(f"[{self.call_id}] Greeting done ({time.time() - t0:.2f}s), listening...")
        self.greeting_done = True

    async def stop(self):
        """Завершение сессии."""
        self.is_active = False
        if self.playback:
            self.playback.close()

        dur = (datetime.now() - self.started_at).total_seconds()
        logger.info(f"[{self.call_id}] Call ended: {dur:.1f}s, "
                     f"{self.total_turns} turns, ctx={len(self.messages)}")

        # Telegram
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

        # JSON log
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
        for engine in [self.asr_engine, self.llm_engine]:
            if engine:
                try:
                    await engine.close()
                except Exception:
                    pass

    async def handle_audio(self, pcm_data: bytes):
        """Обработка аудио-чанков."""
        if not self.greeting_done:
            return

        # Barge-in во время проигрывания
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

        # VAD
        event, audio = self.vad.feed(pcm_data)

        if event == "speech_start":
            logger.info(f"[{self.call_id}] >>> Speech")
        elif event == "speech_end":
            logger.info(f"[{self.call_id}] <<< End ({len(audio)} bytes)")
            asyncio.create_task(self._process_speech(audio))

    async def _process_speech(self, audio_data: bytes):
        """ASR → LLM (выбор файла) → play WAV."""
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

        # === LLM → имя файла ===
        tl = time.time()
        try:
            raw_answer = await self.llm_engine.chat(self.messages)
            chosen_file = raw_answer.strip().strip('"').strip("'").strip()
        except Exception as e:
            logger.error(f"[{self.call_id}] LLM err: {e}")
            return
        llm_ms = int((time.time() - tl) * 1000)

        logger.info(f"[{self.call_id}] LLM ({llm_ms}ms): \"{chosen_file}\"")
        self.messages.append({"role": "assistant", "content": chosen_file})

        # Storage: ответ бота
        if self.storage:
            asyncio.create_task(self.storage.on_bot_response(
                call_id=self.call_id,
                text=chosen_file,
                llm_provider=self.cfg["llm"].get("provider", "yandex"),
                llm_latency_ms=llm_ms,
            ))

        # === Play WAV ===
        if chosen_file in self._tracks:
            pcm, rate = self._tracks[chosen_file]
            logger.info(f"[{self.call_id}] Playing {chosen_file} ({len(pcm)} bytes)")
            self.transcript.append(f"\U0001f916Bot: [{chosen_file}]")

            if self.is_active and not self.barge_in_triggered:
                await self.playback.play_pcm(pcm, rate)
        else:
            logger.warning(f"[{self.call_id}] LLM returned unknown file: \"{chosen_file}\". "
                            f"Available: {list(self._tracks.keys())}")
            self.transcript.append(f"\U0001f916Bot: [unknown: {chosen_file}]")

        total_ms = int((time.time() - t0) * 1000)
        logger.info(f"[{self.call_id}] LLM-Script: {total_ms}ms total, "
                     f"ASR={asr_ms}ms, LLM={llm_ms}ms, file={chosen_file}")
