--
-- ws_bridge.lua
-- Мост между SIP-звонком FreeSWITCH и WebSocket-сервером робота.
--
-- Как работает:
--   1. Отвечает на звонок
--   2. Создаёт FIFO (named pipe) для аудиопотока
--   3. Запускает audio_bridge.py (читает FIFO → шлёт в WebSocket)
--   4. Записывает голос звонящего в FIFO через record
--   5. При завершении звонка — чистит за собой
--
-- Dialplan:
--   <action application="lua" data="ws_bridge.lua 5200"/>
--

local ws_port = argv[1] or "5200"
local uuid = session:getVariable("uuid")
local fifo = "/tmp/voice_pipeline/" .. uuid .. ".raw"

freeswitch.consoleLog("INFO",
    "[ws_bridge] " .. uuid .. " -> ws://127.0.0.1:" .. ws_port .. "\n")

-- Отвечаем на звонок
session:answer()
session:sleep(200)

-- Записываем только голос звонящего (не playback)
session:setVariable("RECORD_READ_ONLY", "true")

-- Создаём FIFO
os.execute("mkfifo " .. fifo .. " 2>/dev/null")

-- Запускаем Python-мост в фоне
os.execute(
    "python3 /usr/share/freeswitch/scripts/audio_bridge.py "
    .. uuid .. " " .. ws_port .. " " .. fifo .. " &"
)

-- Даём мосту время подключиться к WebSocket и открыть FIFO
session:sleep(500)

-- Записываем аудио в FIFO (блокирует до конца звонка)
-- .raw → mod_native_file → сырой PCM 8kHz 16-bit mono
session:execute("record", fifo)

-- Cleanup
freeswitch.consoleLog("INFO", "[ws_bridge] " .. uuid .. " ended\n")
os.execute("rm -f " .. fifo .. " 2>/dev/null")
