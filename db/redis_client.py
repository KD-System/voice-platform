"""
Redis — активные сессии, кэш, pub/sub.

Структуры:
  call:{call_id}          — HASH: состояние активной сессии (TTL 30 мин)
  call:{call_id}:history  — LIST: история диалога для LLM-контекста (TTL 30 мин)
  scenario:{name}         — STRING: кэшированный JSON сценария (TTL 5 мин)
  call_events             — PUB/SUB: события звонков в реальном времени
"""
import json
import logging

import redis.asyncio as aioredis

logger = logging.getLogger("db.redis")

SESSION_TTL = 1800      # 30 минут
SCENARIO_CACHE_TTL = 300  # 5 минут


class RedisClient:
    """Async-клиент Redis."""

    def __init__(self, url: str = "redis://localhost:6379/0"):
        self._url = url
        self._redis: aioredis.Redis | None = None

    async def connect(self):
        self._redis = aioredis.from_url(
            self._url, decode_responses=True, max_connections=20)
        await self._redis.ping()
        logger.info("Redis connected")

    async def close(self):
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis disconnected")

    # ── Активные сессии ──────────────────────────────────────────

    async def create_session(self, call_id: str, *, state: str = "active",
                             mode: str = "pipeline",
                             robot_name: str = "",
                             language: str = "ru",
                             scenario_id: str = "",
                             caller: str = "unknown"):
        """Создать запись об активной сессии звонка."""
        key = f"call:{call_id}"
        mapping = {
            "state": state,
            "mode": mode,
            "robot_name": robot_name,
            "language": language,
            "scenario_id": scenario_id,
            "caller": caller,
            "turns": "0",
            "barge_ins": "0",
        }
        await self._redis.hset(key, mapping=mapping)
        await self._redis.expire(key, SESSION_TTL)

    async def update_session(self, call_id: str, **fields):
        """Обновить поля активной сессии."""
        key = f"call:{call_id}"
        if fields:
            await self._redis.hset(key, mapping={
                k: str(v) for k, v in fields.items()
            })

    async def get_session(self, call_id: str) -> dict | None:
        """Получить состояние активной сессии."""
        key = f"call:{call_id}"
        data = await self._redis.hgetall(key)
        return data if data else None

    async def end_session(self, call_id: str):
        """Завершить сессию: пометить и дать TTL для cleanup."""
        key = f"call:{call_id}"
        await self._redis.hset(key, "state", "ended")
        await self._redis.expire(key, 60)  # удалить через минуту

    # ── История диалога ──────────────────────────────────────────

    async def push_message(self, call_id: str, message: dict):
        """Добавить сообщение в историю диалога."""
        key = f"call:{call_id}:history"
        await self._redis.rpush(key, json.dumps(message, ensure_ascii=False))
        await self._redis.expire(key, SESSION_TTL)

    async def get_history(self, call_id: str) -> list[dict]:
        """Получить всю историю диалога."""
        key = f"call:{call_id}:history"
        items = await self._redis.lrange(key, 0, -1)
        return [json.loads(item) for item in items]

    async def get_recent_history(self, call_id: str,
                                 count: int = 10) -> list[dict]:
        """Получить последние N сообщений."""
        key = f"call:{call_id}:history"
        items = await self._redis.lrange(key, -count, -1)
        return [json.loads(item) for item in items]

    # ── Кэш сценариев ───────────────────────────────────────────

    async def cache_scenario(self, name: str, data: dict):
        """Кэшировать сценарий."""
        key = f"scenario:{name}"
        await self._redis.set(
            key, json.dumps(data, ensure_ascii=False), ex=SCENARIO_CACHE_TTL)

    async def get_cached_scenario(self, name: str) -> dict | None:
        """Получить сценарий из кэша."""
        key = f"scenario:{name}"
        raw = await self._redis.get(key)
        return json.loads(raw) if raw else None

    # ── Pub/Sub события ──────────────────────────────────────────

    async def publish_event(self, event_type: str, data: dict):
        """Опубликовать событие в канал call_events."""
        payload = {"type": event_type, **data}
        await self._redis.publish(
            "call_events", json.dumps(payload, ensure_ascii=False))

    def subscribe_events(self):
        """Вернуть pubsub-объект для подписки на события."""
        pubsub = self._redis.pubsub()
        return pubsub

    # ── Статистика ───────────────────────────────────────────────

    async def get_active_calls_count(self) -> int:
        """Количество активных звонков (приблизительное)."""
        count = 0
        async for key in self._redis.scan_iter("call:*", count=100):
            if ":history" not in key:
                state = await self._redis.hget(key, "state")
                if state == "active":
                    count += 1
        return count
