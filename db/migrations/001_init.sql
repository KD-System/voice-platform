-- Voice Platform — начальная миграция
-- PostgreSQL: таблицы calls, scenarios, users

BEGIN;

-- ── Звонки ──────────────────────────────────────────────────────
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

COMMENT ON TABLE calls IS 'Метаданные телефонных звонков';
COMMENT ON COLUMN calls.call_id IS 'Внутренний ID звонка (call-0001, ...)';
COMMENT ON COLUMN calls.uuid IS 'UUID от FreeSWITCH';
COMMENT ON COLUMN calls.mode IS 'Режим: pipeline, realtime, llm_script';
COMMENT ON COLUMN calls.status IS 'active, completed, error, dropped';

-- ── Сценарии ────────────────────────────────────────────────────
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

COMMENT ON TABLE scenarios IS 'Сценарии (профили) роботов';
COMMENT ON COLUMN scenarios.config_json IS 'Полная конфигурация робота в JSON';

-- ── Пользователи (админ-панель) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(128) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    role          VARCHAR(32)  NOT NULL DEFAULT 'viewer',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE users IS 'Пользователи админ-панели';
COMMENT ON COLUMN users.role IS 'admin, editor, viewer';

COMMIT;
