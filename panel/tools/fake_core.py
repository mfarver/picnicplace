"""Stand in for the core on the panel's serial link, to develop the panel without a machine.

Sends the panel a ``state`` message twice a second, with the position moving, and prints
every line the panel sends (its JSON messages, and over UART its log too). Type a line
and press Enter to send it as a ``message`` to the panel; type ``quiet`` to stop sending
state (the panel should say it has no connection after two seconds), ``loud`` to resume,
``backlight 40`` to set the backlight's brightness (0-100).

    python panel/tools/fake_core.py /dev/cu.usbmodem5C940614361

It needs pyserial (ESP-IDF's Python environment has it).
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time

import serial


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

    def read() -> None:
        buffer = b""
        while True:
            buffer += port.read(256)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode(errors="replace").rstrip("\r")
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
        elif text.startswith("backlight "):
            send({"type": "backlight", "level": int(text.split()[1])})
        elif text:
            send({"type": "message", "level": "error", "text": text})


if __name__ == "__main__":
    main()
