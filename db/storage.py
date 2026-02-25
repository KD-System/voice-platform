"""
Единый Storage API — фасад для PostgreSQL + MongoDB + Redis.

Упрощает интеграцию: один объект на всю платформу.
Graceful degradation: если какой-то бэкенд недоступен — логируем ошибку,
но не роняем звонок.
"""
import logging
from datetime import datetime, timezone

from .postgres import PostgresClient
from .mongo import MongoClient
from .redis_client import RedisClient

logger = logging.getLogger("db.storage")


class Storage:
    """
    Единая точка доступа к хранилищам.

    Использование:
        storage = Storage.from_config(cfg)
        await storage.connect()
        ...
        await storage.close()
    """

    def __init__(self, *, pg: PostgresClient, mongo: MongoClient,
                 redis: RedisClient):
        self.pg = pg
        self.mongo = mongo
        self.redis = redis
        self._connected = False

    @classmethod
    def from_config(cls, cfg: dict) -> "Storage":
        """Создать Storage из конфигурации платформы."""
        db_cfg = cfg.get("db", {})

        pg_dsn = db_cfg.get("postgres_dsn",
                            "postgresql://voice:voice@localhost:5432/voice_platform")
        mongo_uri = db_cfg.get("mongo_uri",
                               "mongodb://voice:voice@localhost:27017")
        mongo_db = db_cfg.get("mongo_database", "voice_platform")
        redis_url = db_cfg.get("redis_url", "redis://localhost:6379/0")

        return cls(
            pg=PostgresClient(pg_dsn),
            mongo=MongoClient(mongo_uri, mongo_db),
            redis=RedisClient(redis_url),
        )

    async def connect(self):
        """Подключить все хранилища."""
        errors = []
        for name, client in [("PostgreSQL", self.pg),
                              ("MongoDB", self.mongo),
                              ("Redis", self.redis)]:
            try:
                await client.connect()
            except Exception as e:
                logger.error(f"{name} connection failed: {e}")
                errors.append(name)

        self._connected = True
        if errors:
            logger.warning(f"Storage partially connected (failed: {errors})")
        else:
            logger.info("Storage fully connected (PG + Mongo + Redis)")

    async def close(self):
        """Отключить все хранилища."""
        for name, client in [("PostgreSQL", self.pg),
                              ("MongoDB", self.mongo),
                              ("Redis", self.redis)]:
            try:
                await client.close()
            except Exception as e:
                logger.error(f"{name} close error: {e}")
        self._connected = False

    # ── Высокоуровневые операции звонков ──────────────────────────

    async def on_call_start(self, *, call_id: str, uuid: str,
                            caller: str, mode: str,
                            robot_name: str, language: str = "ru",
                            scenario_id: str = ""):
        """
        Начало звонка — записать во все хранилища.

        PG:    INSERT INTO calls
        Mongo: создать документ транскрипции
        Redis: создать активную сессию + pub/sub event
        """
        # PostgreSQL
        try:
            await self.pg.insert_call(
                call_id=call_id, uuid=uuid, caller=caller,
                scenario_id=scenario_id or None, mode=mode,
                robot_name=robot_name, language=language,
            )
        except Exception as e:
            logger.error(f"[{call_id}] PG insert_call: {e}")

        # MongoDB
        try:
            await self.mongo.create_transcription(
                call_id=call_id, language=language)
        except Exception as e:
            logger.error(f"[{call_id}] Mongo create_transcription: {e}")

        # Redis
        try:
            await self.redis.create_session(
                call_id, mode=mode, robot_name=robot_name,
                language=language, scenario_id=scenario_id,
                caller=caller,
            )
            await self.redis.publish_event("call_started", {
                "call_id": call_id,
                "caller": caller,
                "mode": mode,
                "robot_name": robot_name,
            })
        except Exception as e:
            logger.error(f"[{call_id}] Redis create_session: {e}")

    async def on_user_speech(self, *, call_id: str, text: str,
                             confidence: float = 0.0,
                             asr_provider: str = "",
                             asr_latency_ms: int = 0):
        """Пользователь сказал реплику — записать сегмент + pipeline step."""
        now = datetime.now(timezone.utc)
        segment = {
            "role": "user",
            "text": text,
            "confidence": confidence,
            "asr_provider": asr_provider,
            "asr_latency_ms": asr_latency_ms,
            "timestamp": now,
        }
        step = {
            "step": "asr",
            "duration_ms": asr_latency_ms,
            "provider": asr_provider,
            "result": "ok" if text else "empty",
        }

        # MongoDB
        try:
            await self.mongo.add_segment(call_id, segment)
            await self.mongo.add_pipeline_step(call_id, step)
        except Exception as e:
            logger.error(f"[{call_id}] Mongo user_speech: {e}")

        # Redis — история для LLM
        try:
            await self.redis.push_message(call_id, {
                "role": "user", "text": text,
            })
            await self.redis.update_session(call_id,
                                            turns=str(asr_latency_ms))
        except Exception as e:
            logger.error(f"[{call_id}] Redis push_message: {e}")

    async def on_bot_response(self, *, call_id: str, text: str,
                              llm_provider: str = "",
                              llm_latency_ms: int = 0,
                              tts_provider: str = "",
                              tts_latency_ms: int = 0):
        """Бот ответил — записать сегмент + pipeline steps."""
        now = datetime.now(timezone.utc)
        segment = {
            "role": "assistant",
            "text": text,
            "llm_provider": llm_provider,
            "llm_latency_ms": llm_latency_ms,
            "tts_provider": tts_provider,
            "tts_latency_ms": tts_latency_ms,
            "timestamp": now,
        }

        # MongoDB
        try:
            await self.mongo.add_segment(call_id, segment)
            await self.mongo.add_pipeline_step(call_id, {
                "step": "llm",
                "duration_ms": llm_latency_ms,
                "provider": llm_provider,
                "result": "ok",
            })
            if tts_provider:
                await self.mongo.add_pipeline_step(call_id, {
                    "step": "tts",
                    "duration_ms": tts_latency_ms,
                    "provider": tts_provider,
                    "result": "ok",
                })
        except Exception as e:
            logger.error(f"[{call_id}] Mongo bot_response: {e}")

        # Redis
        try:
            await self.redis.push_message(call_id, {
                "role": "assistant", "text": text,
            })
        except Exception as e:
            logger.error(f"[{call_id}] Redis push_message: {e}")

    async def on_barge_in(self, *, call_id: str):
        """Пользователь перебил бота."""
        try:
            await self.mongo.add_pipeline_step(call_id, {
                "step": "barge_in",
                "duration_ms": 0,
                "provider": "vad",
                "result": "interrupted",
            })
        except Exception as e:
            logger.error(f"[{call_id}] Mongo barge_in: {e}")

        try:
            session = await self.redis.get_session(call_id)
            if session:
                count = int(session.get("barge_ins", "0")) + 1
                await self.redis.update_session(call_id, barge_ins=count)
        except Exception as e:
            logger.error(f"[{call_id}] Redis barge_in: {e}")

    async def on_call_end(self, *, call_id: str, duration_sec: float,
                          turns: int, barge_ins: int,
                          status: str = "completed"):
        """
        Завершение звонка — финализировать все хранилища.

        PG:    UPDATE calls SET ended_at, duration, status
        Mongo: обновить metadata.total_duration_ms
        Redis: пометить сессию ended + pub/sub event
        """
        # PostgreSQL
        try:
            await self.pg.finish_call(
                call_id, duration_sec=duration_sec,
                turns=turns, barge_ins=barge_ins, status=status,
            )
        except Exception as e:
            logger.error(f"[{call_id}] PG finish_call: {e}")

        # MongoDB
        try:
            await self.mongo.finish_transcription(
                call_id, total_duration_ms=int(duration_sec * 1000))
        except Exception as e:
            logger.error(f"[{call_id}] Mongo finish_transcription: {e}")

        # Redis
        try:
            await self.redis.end_session(call_id)
            await self.redis.publish_event("call_ended", {
                "call_id": call_id,
                "duration_sec": duration_sec,
                "turns": turns,
                "status": status,
            })
        except Exception as e:
            logger.error(f"[{call_id}] Redis end_session: {e}")
