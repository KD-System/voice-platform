"""
MongoDB — транскрипции звонков.

Документная модель с вложенными массивами сегментов и pipeline-логов.
Шардирование по call_id.
"""
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("db.mongo")

COLLECTION = "transcriptions"


class MongoClient:
    """Async-клиент MongoDB через motor."""

    def __init__(self, uri: str, database: str = "voice_platform"):
        self._uri = uri
        self._db_name = database
        self._client: AsyncIOMotorClient | None = None
        self._db = None

    async def connect(self):
        """Подключиться и создать индексы."""
        self._client = AsyncIOMotorClient(self._uri)
        self._db = self._client[self._db_name]
        await self._ensure_indexes()
        logger.info("MongoDB connected")

    async def close(self):
        if self._client:
            self._client.close()
            logger.info("MongoDB disconnected")

    async def _ensure_indexes(self):
        """Создать индексы для эффективных запросов."""
        col = self._db[COLLECTION]
        await col.create_index("call_id", unique=True)
        await col.create_index("started_at")
        await col.create_index("metadata.language")
        logger.info("MongoDB indexes ready")

    # ── Транскрипции ─────────────────────────────────────────────

    async def create_transcription(self, *, call_id: str,
                                   language: str = "ru") -> str:
        """Создать документ транскрипции при начале звонка."""
        doc = {
            "call_id": call_id,
            "segments": [],
            "pipeline_log": [],
            "metadata": {
                "language": language,
                "total_duration_ms": 0,
                "turns_count": 0,
            },
            "started_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        result = await self._db[COLLECTION].insert_one(doc)
        return str(result.inserted_id)

    async def add_segment(self, call_id: str, segment: dict):
        """
        Добавить сегмент диалога в транскрипцию.

        segment пример (user):
        {
            "role": "user",
            "text": "какой у меня баланс",
            "confidence": 0.94,
            "asr_provider": "yandex",
            "asr_latency_ms": 280,
            "timestamp": datetime.now(timezone.utc),
        }

        segment пример (assistant):
        {
            "role": "assistant",
            "text": "Ваш баланс составляет пятьсот рублей.",
            "llm_provider": "yandex_gpt",
            "llm_latency_ms": 310,
            "tts_provider": "yandex",
            "tts_latency_ms": 180,
            "timestamp": datetime.now(timezone.utc),
        }
        """
        if "timestamp" not in segment:
            segment["timestamp"] = datetime.now(timezone.utc)

        await self._db[COLLECTION].update_one(
            {"call_id": call_id},
            {
                "$push": {"segments": segment},
                "$inc": {"metadata.turns_count": 1},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

    async def add_pipeline_step(self, call_id: str, step: dict):
        """
        Добавить шаг пайплайна в лог.

        step пример:
        {
            "step": "asr",
            "duration_ms": 280,
            "provider": "yandex",
            "result": "ok",
            "turn": 1,
        }
        """
        await self._db[COLLECTION].update_one(
            {"call_id": call_id},
            {
                "$push": {"pipeline_log": step},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

    async def finish_transcription(self, call_id: str, *,
                                   total_duration_ms: int):
        """Обновить метаданные при завершении звонка."""
        await self._db[COLLECTION].update_one(
            {"call_id": call_id},
            {
                "$set": {
                    "metadata.total_duration_ms": total_duration_ms,
                    "updated_at": datetime.now(timezone.utc),
                },
            },
        )

    async def get_transcription(self, call_id: str) -> dict | None:
        """Получить полную транскрипцию звонка."""
        doc = await self._db[COLLECTION].find_one(
            {"call_id": call_id}, {"_id": 0})
        return doc

    async def list_transcriptions(self, *, limit: int = 50,
                                  offset: int = 0) -> list[dict]:
        """Список транскрипций (без segments для экономии)."""
        cursor = self._db[COLLECTION].find(
            {},
            {"_id": 0, "call_id": 1, "metadata": 1,
             "started_at": 1, "updated_at": 1},
        ).sort("started_at", -1).skip(offset).limit(limit)
        return await cursor.to_list(length=limit)

    async def search_segments(self, text_query: str, *,
                              limit: int = 20) -> list[dict]:
        """Полнотекстовый поиск по сегментам."""
        cursor = self._db[COLLECTION].find(
            {"segments.text": {"$regex": text_query, "$options": "i"}},
            {"_id": 0, "call_id": 1, "segments": 1, "metadata": 1},
        ).limit(limit)
        return await cursor.to_list(length=limit)
