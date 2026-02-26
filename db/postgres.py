"""
PostgreSQL — структурированные данные.

Таблицы:
  - calls:      метаданные звонков
  - scenarios:  сценарии роботов
  - users:      пользователи админ-панели
"""
import logging
from datetime import datetime

import asyncpg

logger = logging.getLogger("db.postgres")


class PostgresClient:
    """Async-клиент PostgreSQL через asyncpg."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self):
        """Создать пул соединений и применить миграции."""
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        await self._run_migrations()
        logger.info("PostgreSQL connected")

    async def close(self):
        if self._pool:
            await self._pool.close()
            logger.info("PostgreSQL disconnected")

    # ── Миграции ─────────────────────────────────────────────────

    async def _run_migrations(self):
        """Создать таблицы, если не существуют."""
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("PostgreSQL schema ready")

    # ── Calls ────────────────────────────────────────────────────

    async def insert_call(self, *, call_id: str, uuid: str, caller: str,
                          scenario_id: str | None, mode: str,
                          robot_name: str, language: str) -> int:
        """Создать запись о начале звонка. Возвращает id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO calls
                   (call_id, uuid, caller, scenario_id, mode, robot_name, language)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   RETURNING id""",
                call_id, uuid, caller, scenario_id, mode, robot_name, language,
            )
            return row["id"]

    async def finish_call(self, call_id: str, *, duration_sec: float,
                          turns: int, barge_ins: int,
                          status: str = "completed"):
        """Обновить запись при завершении звонка."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE calls
                   SET ended_at = NOW(),
                       duration_sec = $2,
                       turns = $3,
                       barge_ins = $4,
                       status = $5
                   WHERE call_id = $1""",
                call_id, duration_sec, turns, barge_ins, status,
            )

    async def get_call(self, call_id: str) -> dict | None:
        """Получить звонок по call_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM calls WHERE call_id = $1", call_id)
            return dict(row) if row else None

    async def list_calls(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        """Список звонков с пагинацией (новые первые)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM calls
                   ORDER BY started_at DESC
                   LIMIT $1 OFFSET $2""",
                limit, offset,
            )
            return [dict(r) for r in rows]

    # ── Scenarios ────────────────────────────────────────────────

    async def upsert_scenario(self, *, name: str, mode: str,
                              system_prompt: str,
                              config_json: dict,
                              tts_voice: str = "",
                              language: str = "ru") -> int:
        """Создать/обновить сценарий. Возвращает id."""
        import json
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO scenarios (name, mode, system_prompt, config_json, tts_voice, language)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                   ON CONFLICT (name)
                   DO UPDATE SET mode = $2,
                                 system_prompt = $3,
                                 config_json = $4::jsonb,
                                 tts_voice = $5,
                                 language = $6,
                                 updated_at = NOW()
                   RETURNING id""",
                name, mode, system_prompt, json.dumps(config_json), tts_voice, language,
            )
            return row["id"]

    async def get_scenario(self, name: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM scenarios WHERE name = $1", name)
            return dict(row) if row else None

    async def list_scenarios(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM scenarios ORDER BY name")
            return [dict(r) for r in rows]

    # ── Users ────────────────────────────────────────────────────

    async def create_user(self, *, username: str, password_hash: str,
                          role: str = "viewer") -> int:
        """Создать пользователя. Возвращает id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO users (username, password_hash, role)
                   VALUES ($1, $2, $3)
                   RETURNING id""",
                username, password_hash, role,
            )
            return row["id"]

    async def get_user(self, username: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE username = $1", username)
            return dict(row) if row else None

    async def list_users(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, username, role, created_at FROM users ORDER BY username")
            return [dict(r) for r in rows]


# ── SQL-схема (auto-migrate) ────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS calls (
    id            SERIAL PRIMARY KEY,
    call_id       VARCHAR(64)  NOT NULL UNIQUE,
    uuid          VARCHAR(64),
    caller        VARCHAR(64)  NOT NULL DEFAULT 'unknown',
    scenario_id   VARCHAR(128),
    mode          VARCHAR(32)  NOT NULL DEFAULT 'pipeline',
    robot_name    VARCHAR(128) NOT NULL DEFAULT '',
    language      VARCHAR(16)  NOT NULL DEFAULT 'ru',
    status        VARCHAR(32)  NOT NULL DEFAULT 'active',
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ,
    duration_sec  REAL,
    turns         INTEGER      DEFAULT 0,
    barge_ins     INTEGER      DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_calls_started ON calls (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_caller  ON calls (caller);
CREATE INDEX IF NOT EXISTS idx_calls_status  ON calls (status);

CREATE TABLE IF NOT EXISTS scenarios (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(128) NOT NULL UNIQUE,
    mode          VARCHAR(32)  NOT NULL DEFAULT 'pipeline',
    system_prompt TEXT         NOT NULL DEFAULT '',
    config_json   JSONB        NOT NULL DEFAULT '{}',
    tts_voice     VARCHAR(64)  DEFAULT '',
    language      VARCHAR(16)  NOT NULL DEFAULT 'ru',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(128) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    role          VARCHAR(32)  NOT NULL DEFAULT 'viewer',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
"""
