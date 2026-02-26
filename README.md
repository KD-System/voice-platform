# Voice Platform v3

Платформа голосовых роботов с поддержкой телефонии (FreeSWITCH) и веб-интерфейса.

## Архитектура

```
 Телефон (SIP)                    Браузер
      │                               │
      ▼                               ▼
 FreeSWITCH ──WebSocket──►  run.py   web.py (FastAPI)
                               │        │
                   ┌───────────┴────────┘
                   ▼
            SessionClass (по mode из config.json)
            ├── pipeline:   VAD → ASR → LLM(stream) → TTS → play
            ├── realtime:   Yandex Realtime API (full-duplex)
            └── llm_script: VAD → ASR → LLM выбирает WAV → play
                   │
                   ▼
            db/Storage (неблокирующие записи)
            ├── PostgreSQL — метаданные звонков
            ├── MongoDB    — транскрипции + сегменты
            └── Redis      — активные сессии, pub/sub
```

### Поток одной реплики (pipeline)

```
микрофон → PCM 8kHz → VAD(speech_end) → ASR(Yandex) → текст
  → LLM(YandexGPT, stream) → предложения → TTS → PCM → динамик
```

## Режимы работы

| Режим | Описание | Когда использовать |
|-------|----------|--------------------|
| `pipeline` | VAD → ASR → LLM(stream) → TTS | Свободный диалог, генерация ответов в реальном времени |
| `realtime` | Yandex Realtime API (full-duplex) | Минимальная задержка, серверный VAD |
| `llm_script` | VAD → ASR → LLM выбирает WAV-файл | Скриптовые сценарии с заготовленными аудио-ответами |

## Структура проекта

```
voice-platform/
├── run.py                      # точка входа (FreeSWITCH → WebSocket)
├── web.py                      # веб-демо (браузер → WebSocket, FastAPI)
├── docker-compose.yml          # все сервисы
├── Dockerfile                  # образ приложения (python:3.12-slim)
│
├── core/
│   ├── config.py               # загрузка конфига (config.json + .env + дефолты)
│   ├── vad.py                  # EnergyVAD — детекция речи по RMS энергии
│   ├── audio.py                # load_wav, downsample, compute_rms
│   ├── playback.py             # FSPlayback — воспроизведение через FreeSWITCH
│   ├── logging/
│   │   ├── call_logger.py      # JSON-логи в robots/*/logs/
│   │   └── telegram.py         # отчёты в Telegram
│   └── sessions/
│       ├── session_pipeline.py
│       ├── session_realtime.py
│       └── session_llm_script.py
│
├── asr/                        # провайдеры ASR
│   └── yandex_asr.py           # Yandex SpeechKit STT
│
├── llm/                        # провайдеры LLM
│   └── yandex_llm.py           # YandexGPT (stream + sentence-streaming)
│
├── tts/                        # провайдеры TTS
│   ├── yandex_tts.py           # Yandex SpeechKit
│   ├── elevenlabs_tts.py       # ElevenLabs (streaming, SOCKS5 proxy)
│   └── zvukogram_tts.py        # Zvukogram
│
├── db/
│   ├── storage.py              # единый фасад Storage
│   ├── postgres.py             # asyncpg — calls, scenarios, users
│   ├── mongo.py                # motor — transcriptions, segments, pipeline_log
│   ├── redis_client.py         # redis.asyncio — sessions, cache, pub/sub
│   └── migrations/
│       ├── 001_init.sql        # PostgreSQL схема
│       └── 002_mongo_indexes.js
│
├── robots/                     # конфигурации роботов
│   ├── pipeline_russian/       # config.json + prompt.txt + greeting.wav
│   ├── realtime_russian/
│   └── llm_script_russian/     # + tracks/*.wav
│
├── freeswitch/
│   ├── Dockerfile
│   ├── scripts/audio_bridge.py # FIFO → WebSocket мост
│   ├── scripts/ws_bridge.lua   # Lua dialplan
│   └── conf/dialplan/          # маршрутизация номеров
│
└── .env                        # секреты (API-ключи, DSN)
```

## Быстрый старт

### 1. Настройка

```bash
cp .env.example .env
# Заполните YANDEX_API_KEY и YANDEX_FOLDER_ID
```

### 2. Запуск

```bash
docker compose up --build -d
```

### 3. Проверка

- **Веб-демо:** http://localhost:8000
- **Логи:** `docker logs voice-platform-web-1 -f`

### Порты

| Сервис | Порт |
|--------|------|
| Web (pipeline) | 8000 |
| Web (llm_script) | 8001 |
| Web (realtime) | 8002 |
| Robot (pipeline) | 5200 |
| Robot (llm_script) | 5201 |
| Robot (realtime) | 5202 |
| FreeSWITCH SIP | 5060 |
| PostgreSQL | 5432 |
| MongoDB | 27017 |
| Redis | 6379 |

## Конфигурация робота

Каждый робот — папка в `robots/` с файлами:

| Файл | Описание |
|------|----------|
| `config.json` | Основной конфиг: mode, порт, провайдеры ASR/LLM/TTS, VAD |
| `prompt.txt` | Системный промпт для LLM |
| `greeting.wav` | Приветственное аудио (опционально) |
| `.env` | Переопределение секретов (опционально) |
| `tracks/*.wav` | WAV-файлы для режима llm_script |

Приоритет конфигурации: `config.json робота` → `.env робота` → `.env корня` → дефолты

### Пример config.json (pipeline)

```json
{
  "ws_port": 5200,
  "mode": "pipeline",
  "asr": {
    "provider": "yandex",
    "language": "ru-RU"
  },
  "llm": {
    "provider": "yandex",
    "temperature": 0.5,
    "max_tokens": 80
  },
  "tts": {
    "provider": "yandex",
    "voice": "alena",
    "sample_rate": 48000
  },
  "vad": {
    "enabled": true,
    "energy_threshold": 200,
    "silence_frames": 25,
    "min_speech_frames": 5
  },
  "greeting_text": "Здравствуйте! Чем могу помочь?"
}
```

## Провайдеры

### ASR (распознавание речи)

| Провайдер | Ключ | Описание |
|-----------|------|----------|
| `yandex` | `YANDEX_API_KEY` | Yandex SpeechKit STT |

### LLM (генерация ответов)

| Провайдер | Ключ | Описание |
|-----------|------|----------|
| `yandex` | `YANDEX_API_KEY` | YandexGPT с sentence-streaming |

### TTS (синтез речи)

| Провайдер | Ключ | Описание |
|-----------|------|----------|
| `yandex` | `YANDEX_API_KEY` | Yandex SpeechKit (голос: alena, jane и др.) |
| `elevenlabs` | `TTS_API_KEY` | ElevenLabs (многоязычный, streaming, SOCKS5 proxy) |
| `zvukogram` | `TTS_TOKEN` + `TTS_EMAIL` | Zvukogram (русские голоса) |

## Хранилище данных

Все звонки автоматически записываются в 4 хранилища:

### PostgreSQL — метаданные

```sql
SELECT call_id, caller, mode, status, duration_sec, turns
FROM calls ORDER BY started_at DESC LIMIT 10;
```

Таблицы: `calls`, `scenarios`, `users`

### MongoDB — транскрипции

```javascript
db.transcriptions.find({call_id: "web-0001"}).pretty()
```

Документ содержит: `segments[]` (role, text, asr_latency_ms, llm_latency_ms), `pipeline_log[]`, `metadata`

### Redis — активные сессии

```bash
redis-cli -a voice KEYS "call:*"
redis-cli -a voice HGETALL "call:web-0001"
redis-cli -a voice LRANGE "call:web-0001:history" 0 -1
```

TTL: 30 минут для сессий, 5 минут для кэша сценариев. Канал `call_events` для pub/sub.

### JSON-файлы

```
robots/pipeline_russian/logs/20260225_224359_web_web-0001.json
```

## VAD и Barge-in

**EnergyVAD** определяет начало и конец речи по RMS-энергии:

- `energy_threshold` — порог громкости (200 по умолчанию)
- `silence_frames` — кадров тишины для завершения (25 = ~0.8 сек)
- `min_speech_frames` — минимум кадров речи для подтверждения (5)

**Barge-in** — прерывание ответа робота голосом пользователя:
- Во время воспроизведения VAD продолжает анализ входящего аудио
- При обнаружении речи — воспроизведение останавливается, робот слушает

## Переменные окружения

```bash
# Yandex Cloud (обязательно)
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...

# Yandex Realtime API (для mode=realtime)
YANDEX_REALTIME_URL=wss://...

# TTS (если ElevenLabs или Zvukogram)
TTS_API_KEY=...          # ElevenLabs
TTS_TOKEN=...            # Zvukogram
TTS_EMAIL=...            # Zvukogram

# Telegram уведомления (опционально)
TG_TOKEN=...
TG_CHAT_ID=...

# Storage (автоматически для Docker Compose)
POSTGRES_DSN=postgresql://voice:voice@postgres:5432/voice_platform
MONGO_URI=mongodb://voice:voice@mongodb:27017/?authSource=admin
REDIS_URL=redis://:voice@redis:6379/0
```

## Docker Compose

```bash
# Запуск всех сервисов
docker compose up --build -d

# Только базы данных
docker compose up postgres mongodb redis -d

# Логи конкретного сервиса
docker logs voice-platform-web-1 -f

# Просмотр звонков
docker exec -it voice-postgres psql -U voice -d voice_platform \
  -c "SELECT * FROM calls ORDER BY started_at DESC LIMIT 5;"
```

### Сервисы

| Сервис | Образ | Зависимости |
|--------|-------|-------------|
| `postgres` | postgres:16-alpine | — |
| `mongodb` | mongo:7 | — |
| `redis` | redis:7-alpine | — |
| `freeswitch` | custom build | — |
| `robot-pipeline` | custom build | freeswitch, postgres, mongodb, redis |
| `robot-llm-script` | custom build | freeswitch, postgres, mongodb, redis |
| `robot-realtime` | custom build | freeswitch, postgres, mongodb, redis |
| `web` | custom build | postgres, mongodb, redis |
| `web-llm-script` | custom build | postgres, mongodb, redis |
| `web-realtime` | custom build | postgres, mongodb, redis |

## Создание нового робота

1. Скопируйте существующий робот:
   ```bash
   cp -r robots/pipeline_russian robots/my_robot
   ```

2. Отредактируйте `robots/my_robot/config.json` (mode, порт, провайдеры)

3. Напишите промпт в `robots/my_robot/prompt.txt`

4. Запустите:
   ```bash
   # Локально
   python web.py robots/my_robot --port 8003

   # Или через run.py (для FreeSWITCH)
   python run.py robots/my_robot
   ```
