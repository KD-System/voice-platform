#!/usr/bin/env python3
"""
Web-интерфейс — поговори с роботом через браузер.

Запуск:
  python web.py robots/pipeline_russian

Откройте http://localhost:8000 в браузере.
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

PLATFORM_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PLATFORM_ROOT))

from core.config import load_config
from core.vad import EnergyVAD
from core.audio import load_wav
from asr import get_asr
from llm import get_llm
from tts import get_tts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("web")

app = FastAPI()
CFG = None
ROBOT_NAME = ""


def create_engines(cfg: dict):
    """Создать ASR/LLM/TTS из конфига."""
    secrets = cfg["secrets"]
    mode = cfg.get("mode", "pipeline")

    # ASR
    asr_cfg = cfg["asr"]
    if asr_cfg["provider"] == "triton_armenian":
        asr = get_asr(asr_cfg["provider"],
                      server_url=asr_cfg.get("server_url", ""),
                      model_name=asr_cfg.get("model_name", "streaming_asr"))
    else:
        asr = get_asr(asr_cfg["provider"],
                      api_key=secrets["yandex_api_key"],
                      folder_id=secrets["yandex_folder_id"],
                      language=asr_cfg["language"])

    # LLM
    llm_cfg = cfg["llm"]
    llm_engine = get_llm(llm_cfg.get("provider", "yandex"),
                         api_key=secrets["yandex_api_key"],
                         folder_id=secrets["yandex_folder_id"],
                         model=llm_cfg.get("model", ""),
                         temperature=llm_cfg.get("temperature", 0.5),
                         max_tokens=llm_cfg.get("max_tokens", 80))

    # TTS (только для pipeline)
    tts_engine = None
    if mode == "pipeline":
        tts_cfg = cfg["tts"]
        if tts_cfg["provider"] == "zvukogram":
            tts_engine = get_tts("zvukogram",
                                 token=secrets["tts_token"],
                                 email=secrets["tts_email"],
                                 voice=tts_cfg.get("voice", "Ada AM"),
                                 speed=tts_cfg.get("speed", 1.0),
                                 pitch=tts_cfg.get("pitch", 0),
                                 sample_rate=tts_cfg.get("sample_rate", 8000))
        elif tts_cfg["provider"] == "elevenlabs":
            tts_engine = get_tts("elevenlabs",
                                 api_key=secrets["tts_api_key"],
                                 voice_id=tts_cfg.get("voice_id", ""),
                                 model_id=tts_cfg.get("model_id", "eleven_multilingual_v2"),
                                 stability=tts_cfg.get("stability", 0.5),
                                 similarity_boost=tts_cfg.get("similarity_boost", 0.75),
                                 speed=tts_cfg.get("speed", 1.0),
                                 proxy=tts_cfg.get("proxy", ""))
        else:
            tts_engine = get_tts("yandex",
                                 api_key=secrets["tts_api_key"] or secrets["yandex_api_key"],
                                 folder_id=secrets["yandex_folder_id"],
                                 voice=tts_cfg.get("voice", "alena"),
                                 language=tts_cfg.get("language", "ru-RU"))

    return asr, llm_engine, tts_engine


@app.get("/")
async def index():
    return HTMLResponse(HTML.replace("{{ROBOT_NAME}}", ROBOT_NAME))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    cfg = CFG
    mode = cfg.get("mode", "pipeline")

    # Движки
    asr, llm_engine, tts_engine = create_engines(cfg)

    # VAD
    vad_cfg = cfg["vad"]
    vad = EnergyVAD(
        energy_threshold=vad_cfg["energy_threshold"],
        silence_frames=vad_cfg["silence_frames"],
        min_speech_frames=vad_cfg["min_speech_frames"],
        enabled=vad_cfg["enabled"],
    )

    # Контекст LLM
    messages = [{"role": "system", "content": cfg["system_prompt"]}]

    # Для llm_script: загрузить треки
    tracks = {}
    if mode == "llm_script":
        robot_dir = Path(cfg["robot_dir"])
        tracks_dir = robot_dir / "tracks"
        if not tracks_dir.exists():
            tracks_dir = robot_dir
        for wav_file in sorted(tracks_dir.glob("*.wav")):
            if wav_file.name == "greeting.wav":
                continue
            try:
                pcm, rate = load_wav(str(wav_file))
                tracks[wav_file.name] = (pcm, rate)
            except Exception as e:
                logger.warning(f"Failed to load {wav_file.name}: {e}")
        track_names = sorted(tracks.keys())
        if track_names:
            files_list = "\n".join(f"- {name}" for name in track_names)
            messages[0]["content"] += (
                f"\n\nДОСТУПНЫЕ АУДИО-ФАЙЛЫ:\n{files_list}\n\n"
                f"ПРАВИЛО: На каждую реплику пользователя ты ОБЯЗАН ответить "
                f"РОВНО одним именем файла из списка выше. "
                f"Ничего кроме имени файла. Без кавычек, без пояснений."
            )

    is_responding = False

    # Greeting
    greeting_pcm = None
    greeting_rate = 8000
    if cfg.get("greeting_wav"):
        try:
            greeting_pcm, greeting_rate = load_wav(cfg["greeting_wav"])
        except Exception:
            pass

    greeting_text = cfg.get("greeting_text", "")

    try:
        await ws.send_json({"type": "ready"})

        # Приветствие
        if greeting_pcm:
            await ws.send_json({"type": "audio", "sample_rate": greeting_rate})
            await ws.send_bytes(greeting_pcm)
            await ws.send_json({"type": "response_end"})
            if greeting_text:
                messages.append({"role": "assistant", "content": greeting_text})
                await ws.send_json({"type": "transcript", "role": "bot", "text": greeting_text})
        elif greeting_text and tts_engine:
            messages.append({"role": "assistant", "content": greeting_text})
            await ws.send_json({"type": "transcript", "role": "bot", "text": greeting_text})
            r = await tts_engine.synthesize(greeting_text)
            audio = r.get("audio", b"")
            rate = r.get("sample_rate", 48000)
            if audio:
                await ws.send_json({"type": "audio", "sample_rate": rate})
                await ws.send_bytes(audio)
            await ws.send_json({"type": "response_end"})

        await ws.send_json({"type": "listening"})

        # Основной цикл
        while True:
            pcm_data = await ws.receive_bytes()

            if is_responding:
                continue

            event, audio = vad.feed(pcm_data)

            if event == "speech_start":
                await ws.send_json({"type": "speech_start"})
                logger.info(">>> Speech")

            elif event == "speech_end":
                logger.info(f"<<< End ({len(audio)} bytes)")
                is_responding = True
                await ws.send_json({"type": "processing"})

                try:
                    # ASR
                    t0 = time.time()
                    fs_rate = cfg.get("fs_sample_rate", 8000)
                    r = await asr.recognize(audio, sample_rate=fs_rate)
                    text = r.get("text", "")
                    asr_ms = int((time.time() - t0) * 1000)

                    if not text:
                        logger.warning(f"ASR empty ({asr_ms}ms)")
                        is_responding = False
                        await ws.send_json({"type": "listening"})
                        continue

                    logger.info(f'ASR ({asr_ms}ms): "{text}"')
                    messages.append({"role": "user", "content": text})
                    await ws.send_json({"type": "transcript", "role": "user", "text": text})

                    if mode == "pipeline":
                        # LLM → TTS streaming
                        full_response = ""
                        async for sentence in llm_engine.chat_stream_sentences(messages):
                            full_response += (" " if full_response else "") + sentence
                            logger.info(f'LLM: "{sentence[:60]}"')
                            r = await tts_engine.synthesize(sentence)
                            audio_out = r.get("audio", b"")
                            rate = r.get("sample_rate", 48000)
                            if audio_out:
                                await ws.send_json({"type": "audio", "sample_rate": rate})
                                await ws.send_bytes(audio_out)

                        if full_response.strip():
                            messages.append({"role": "assistant", "content": full_response.strip()})
                            await ws.send_json({"type": "transcript", "role": "bot", "text": full_response.strip()})

                    elif mode == "llm_script":
                        # LLM → выбор WAV
                        raw = await llm_engine.chat(messages)
                        chosen = raw.strip().strip('"').strip("'").strip()
                        logger.info(f'LLM chose: "{chosen}"')
                        messages.append({"role": "assistant", "content": chosen})

                        if chosen in tracks:
                            pcm_out, rate = tracks[chosen]
                            await ws.send_json({"type": "audio", "sample_rate": rate})
                            await ws.send_bytes(pcm_out)
                            await ws.send_json({"type": "transcript", "role": "bot", "text": f"[{chosen}]"})
                        else:
                            logger.warning(f"Unknown track: {chosen}")
                            await ws.send_json({"type": "transcript", "role": "bot", "text": f"[unknown: {chosen}]"})

                    await ws.send_json({"type": "response_end"})

                except Exception as e:
                    logger.error(f"Processing error: {e}")

                finally:
                    is_responding = False
                    await ws.send_json({"type": "listening"})

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        for engine in [asr, llm_engine, tts_engine]:
            if engine:
                try:
                    await engine.close()
                except Exception:
                    pass
        logger.info("Session ended")


# ── HTML ──────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ROBOT_NAME}}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f1a;
    color: #e0e0e0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
}
h1 { margin-top: 30px; font-size: 1.3em; color: #888; font-weight: 400; }
#status { margin-top: 10px; font-size: 0.9em; color: #666; min-height: 20px; }
#mic-btn {
    margin-top: 30px; width: 80px; height: 80px; border-radius: 50%;
    border: 3px solid #333; background: #1a1a2e; color: #888;
    font-size: 32px; cursor: pointer; transition: all 0.2s;
    display: flex; align-items: center; justify-content: center;
}
#mic-btn:hover { border-color: #555; }
#mic-btn.recording {
    border-color: #e74c3c; background: #2a1a1e; color: #e74c3c;
    animation: pulse 1.5s infinite;
}
#mic-btn.playing { border-color: #3498db; background: #1a1a2e; color: #3498db; }
@keyframes pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(231,76,60,0.4); }
    50% { box-shadow: 0 0 0 15px rgba(231,76,60,0); }
}
#chat {
    margin-top: 30px; width: 90%; max-width: 500px;
    flex: 1; overflow-y: auto; padding-bottom: 20px;
}
.msg {
    margin: 8px 0; padding: 10px 14px; border-radius: 12px;
    max-width: 80%; font-size: 0.95em; line-height: 1.4;
}
.msg.user { background: #1e3a5f; margin-left: auto; border-bottom-right-radius: 4px; }
.msg.bot  { background: #2a2a3e; border-bottom-left-radius: 4px; }
</style>
</head>
<body>
<h1>{{ROBOT_NAME}}</h1>
<div id="status">Click to start</div>
<button id="mic-btn" onclick="toggle()">&#x1f3a4;</button>
<div id="chat"></div>

<script>
const statusEl = document.getElementById('status');
const micBtn   = document.getElementById('mic-btn');
const chatEl   = document.getElementById('chat');

let ws, audioCtx, stream, processor, source;
let isRecording = false, isPlaying = false;
let pendingRate = 48000;
let audioQueue = [], playingQueue = false;

function setStatus(t) { statusEl.textContent = t; }

function addMsg(role, text) {
    const d = document.createElement('div');
    d.className = 'msg ' + role;
    d.textContent = text;
    chatEl.appendChild(d);
    chatEl.scrollTop = chatEl.scrollHeight;
}

async function toggle() {
    if (isRecording) stopAll(); else await startAll();
}

async function startAll() {
    try {
        audioCtx = new AudioContext();
        stream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true }
        });
        source = audioCtx.createMediaStreamSource(stream);
        processor = audioCtx.createScriptProcessor(4096, 1, 1);
        source.connect(processor);
        processor.connect(audioCtx.destination);

        processor.onaudioprocess = (e) => {
            if (!isRecording || !ws || ws.readyState !== 1 || isPlaying) return;
            const input = e.inputBuffer.getChannelData(0);
            const ratio = audioCtx.sampleRate / 8000;
            const len = Math.floor(input.length / ratio);
            const pcm = new Int16Array(len);
            for (let i = 0; i < len; i++) {
                const s = input[Math.floor(i * ratio)];
                pcm[i] = Math.max(-32768, Math.min(32767, Math.floor(s * 32768)));
            }
            ws.send(pcm.buffer);
        };

        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        ws = new WebSocket(proto + '://' + location.host + '/ws');
        ws.binaryType = 'arraybuffer';

        ws.onopen = () => {
            isRecording = true;
            micBtn.className = 'recording';
            setStatus('Connected');
        };

        ws.onmessage = (e) => {
            if (typeof e.data === 'string') {
                const msg = JSON.parse(e.data);
                if (msg.type === 'ready')        setStatus('Ready');
                else if (msg.type === 'audio')   pendingRate = msg.sample_rate;
                else if (msg.type === 'listening') {
                    if (!isPlaying) { setStatus('Listening...'); micBtn.className = 'recording'; }
                }
                else if (msg.type === 'speech_start')  setStatus('Speaking...');
                else if (msg.type === 'processing')    { setStatus('Thinking...'); micBtn.className = ''; }
                else if (msg.type === 'transcript')     addMsg(msg.role, msg.text);
                else if (msg.type === 'response_end')   {} // audio queue handles state
            } else {
                audioQueue.push({ buffer: e.data, rate: pendingRate });
                drainQueue();
            }
        };

        ws.onclose = () => { setStatus('Disconnected'); isRecording = false; micBtn.className = ''; };
        ws.onerror = () => setStatus('Connection error');
    } catch (err) {
        setStatus('Error: ' + err.message);
    }
}

function stopAll() {
    isRecording = false;
    micBtn.className = '';
    setStatus('Click to start');
    if (processor) { processor.disconnect(); processor = null; }
    if (source) { source.disconnect(); source = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (ws) { ws.close(); ws = null; }
    if (audioCtx) { audioCtx.close(); audioCtx = null; }
    audioQueue = [];
    playingQueue = false;
    isPlaying = false;
}

async function drainQueue() {
    if (playingQueue) return;
    playingQueue = true;
    isPlaying = true;
    micBtn.className = 'playing';
    setStatus('Robot speaks...');

    while (audioQueue.length > 0) {
        const { buffer, rate } = audioQueue.shift();
        await playChunk(buffer, rate);
    }

    isPlaying = false;
    playingQueue = false;
    if (isRecording) { micBtn.className = 'recording'; setStatus('Listening...'); }
}

function playChunk(buf, sampleRate) {
    return new Promise(resolve => {
        const samples = new Int16Array(buf);
        const f32 = new Float32Array(samples.length);
        for (let i = 0; i < samples.length; i++) f32[i] = samples[i] / 32768;
        const ab = audioCtx.createBuffer(1, f32.length, sampleRate);
        ab.getChannelData(0).set(f32);
        const src = audioCtx.createBufferSource();
        src.buffer = ab;
        src.connect(audioCtx.destination);
        src.onended = resolve;
        src.start();
    });
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python web.py <robot_dir> [--port PORT]")
        print("Example: python web.py robots/pipeline_russian --port 8000")
        sys.exit(1)

    robot_path = Path(sys.argv[1])
    if not robot_path.is_absolute():
        robot_path = PLATFORM_ROOT / robot_path

    port = int(os.environ.get("WEB_PORT", 8000))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    CFG = load_config(str(robot_path))
    ROBOT_NAME = robot_path.name

    mode = CFG.get("mode", "pipeline")
    logger.info(f"Web: {ROBOT_NAME} (mode={mode})")
    logger.info(f"Open http://localhost:{port}")

    uvicorn.run(app, host="0.0.0.0", port=port)
