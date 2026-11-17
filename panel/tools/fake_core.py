"""Stand in for the core on the panel's serial link, to develop the panel without a machine.

Sends the panel a ``state`` message twice a second, with the position moving, and prints
every line the panel sends (its JSON messages, and over UART its log too). Type a line
and press Enter to send it as a ``message`` to the panel; type ``quiet`` to stop sending
state (the panel should say it has no connection after two seconds), ``loud`` to resume,
``backlight 40`` to set the backlight's brightness (0-100), ``screenshot FILE.png`` to save
the panel's screen.

    python panel/tools/fake_core.py /dev/cu.usbmodem5C940614361

It needs pyserial (ESP-IDF's Python environment has it).
"""

from __future__ import annotations

import json
import math
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import serial


def write_png(path: Path, width: int, rows: dict[int, bytes]) -> None:
    """Save RGB565 rows (little-endian, as the panel sends them) as an RGB PNG."""
    raw = bytearray()
    for y in range(len(rows)):
        raw.append(0)  # no filter
        row = rows[y]
        for i in range(0, width * 2, 2):
            v = row[i] | (row[i + 1] << 8)
            r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
            raw += bytes(((r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, len(rows), 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: fake_core.py PORT")
    port = serial.Serial(sys.argv[1], 115200, timeout=0.2)
    sending = threading.Event()
    sending.set()
    lock = threading.Lock()

    def send(message: dict[str, object]) -> None:
        with lock:
            port.write((json.dumps(message) + "\n").encode())

    shot: dict[str, object] = {}

    def read() -> None:
        buffer = b""
        while True:
            buffer += port.read(256)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode(errors="replace").rstrip("\r")
                if text.startswith('{"type":"screenshot"'):
                    part = json.loads(text)
                    rows: dict[int, bytes] = shot.setdefault("rows", {})  # type: ignore[assignment]
                    rows[part["row"]] = bytes.fromhex(part["rgb565"])
                    if len(rows) == part["height"]:
                        write_png(Path(str(shot["path"])), part["width"], rows)
                        print("saved", shot["path"], flush=True)
                        shot.clear()
                    continue
                marker = "panel>" if text.startswith("{") else "  log|"
                print(marker, text, flush=True)

    def states() -> None:
        start = time.monotonic()
        while True:
            t = time.monotonic() - start
            if sending.is_set():
                send(
                    {
                        "type": "state",
                        "connected": True,
                        "homed": int(t) % 20 >= 10,
                        "control": "Studio" if int(t) % 30 < 15 else None,
                        "run": ["ready", "running", "paused", "waiting"][int(t / 5) % 4],
                        "position": {
                            "X": 120 + 40 * math.sin(t / 3),
                            "Y": 80 + 30 * math.cos(t / 3),
                            "Z": -1.25,
                            "A": (t * 30) % 360 - 180,
                            "B": 0.0,
                        },
                        "tips": {"Left nozzle": "N045", "Right nozzle": "N08"},
                    }
                )
            time.sleep(0.5)

    threading.Thread(target=read, daemon=True).start()
    threading.Thread(target=states, daemon=True).start()
    for line in sys.stdin:
        text = line.strip()
        if text == "quiet":
            sending.clear()
        elif text == "loud":
            sending.set()
        elif text.startswith("screenshot "):
            shot["path"] = text.split(maxsplit=1)[1]
            send({"type": "screenshot"})
        elif text.startswith("backlight "):
            send({"type": "backlight", "level": int(text.split()[1])})
        elif text:
            send({"type": "message", "level": "error", "text": text})


if __name__ == "__main__":
    main()
