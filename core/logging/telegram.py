"""Telegram-уведомления о звонках."""
import logging
import aiohttp

logger = logging.getLogger("core.telegram")


async def send_telegram(token: str, chat_id: str, text: str):
    """Отправить сообщение в Telegram."""
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    logger.warning(f"TG send failed: {resp.status}")
    except Exception as e:
        logger.warning(f"TG error: {e}")


def format_call_report(caller: str, uuid: str, call_time: str,
                       duration: float, turns: int, barge_ins: int,
                       asr_avg_ms: int, transcript: list[str]) -> str:
    """Сформировать текст отчёта о звонке."""
    header = (
        f"\U0001f4de <b>Call Report</b>\n"
        f"Tel: {caller}\n"
        f"Call time: {call_time}\n"
        f"Call uuid: {uuid}\n"
        f"Duration: {duration:.0f}s | Turns: {turns} | "
        f"Barge-ins: {barge_ins} | ASR avg: {asr_avg_ms}ms\n\n"
        f"\u270d\ufe0f <b>\u0422\u0440\u0430\u043d\u0441\u043a\u0440\u0438\u043f\u0446\u0438\u044f:</b>\n"
    )
    body = "\n".join(transcript)
    return header + body
