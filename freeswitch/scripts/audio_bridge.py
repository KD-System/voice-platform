#!/usr/bin/env python3
"""
Audio bridge: читает raw PCM из FIFO (named pipe) и отправляет
в WebSocket-сервер голосового робота.

Без внешних зависимостей — только стандартная библиотека Python.

Usage: python3 audio_bridge.py <uuid> <ws_port> <fifo_path>
"""
import base64
import os
import socket
import struct
import sys

CHUNK_SIZE = 640  # 40ms @ 8kHz 16-bit mono


# ── Минимальный WebSocket-клиент ────────────────────────────────

def ws_connect(host: str, port: int) -> socket.socket:
    """WebSocket handshake и подключение."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect((host, port))

    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(handshake.encode())

    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)

    status_line = buf.split(b"\r\n")[0]
    if b"101" not in status_line:
        raise ConnectionError(f"WebSocket handshake failed: {status_line}")

    sock.settimeout(None)
    return sock


def ws_frame(data: bytes, opcode: int = 0x02) -> bytes:
    """Собрать masked WebSocket frame (клиент обязан маскировать)."""
    mask_key = os.urandom(4)
    length = len(data)

    if length < 126:
        header = struct.pack("BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)

    masked = bytearray(data)
    for i in range(len(masked)):
        masked[i] ^= mask_key[i % 4]

    return header + mask_key + bytes(masked)


# ── Main ────────────────────────────────────────────────────────

def main():
    uuid = sys.argv[1]
    ws_port = int(sys.argv[2])
    fifo_path = sys.argv[3]

    # Подключаемся к WebSocket-серверу робота
    try:
        ws = ws_connect("127.0.0.1", ws_port)
    except Exception as e:
        print(f"[audio_bridge] connect failed: {e}", file=sys.stderr)
        return

    # Отправляем UUID вызова (текстовый фрейм)
    ws.sendall(ws_frame(uuid.encode(), opcode=0x01))

    # Читаем из FIFO и стримим в WebSocket
    try:
        with open(fifo_path, "rb") as fifo:
            while True:
                chunk = fifo.read(CHUNK_SIZE)
                if not chunk:
                    break
                ws.sendall(ws_frame(chunk, opcode=0x02))
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    finally:
        try:
            ws.sendall(ws_frame(struct.pack("!H", 1000), opcode=0x08))
        except Exception:
            pass
        ws.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: audio_bridge.py <uuid> <ws_port> <fifo_path>",
              file=sys.stderr)
        sys.exit(1)
    main()
