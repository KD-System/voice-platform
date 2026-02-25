"""JSON-логирование звонков."""
import json
import logging
from pathlib import Path

logger = logging.getLogger("core.call_logger")


def save_call_log(robot_dir: str, uuid: str, caller: str, call_time: str,
                  duration: float, turns: int, barge_ins: int,
                  turn_metrics: list, transcript: list):
    """Сохранить JSON-лог звонка в logs/ директорию робота."""
    logs_dir = Path(robot_dir) / "logs"
    logs_dir.mkdir(exist_ok=True)

    log_data = {
        "uuid": uuid,
        "caller": caller,
        "call_time": call_time,
        "duration_sec": round(duration, 1),
        "turns": turns,
        "barge_ins": barge_ins,
        "asr_details": turn_metrics,
        "transcript": transcript,
    }

    # Имя файла: дата_номер_uuid.json
    safe_time = call_time.replace(" ", "_").replace(":", "").replace("-", "")
    fn = f"{safe_time}_{caller}_{(uuid or 'x')[:8]}.json"
    path = logs_dir / fn

    try:
        path.write_text(json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Log saved: {fn}")
    except Exception as e:
        logger.error(f"Log save error: {e}")
