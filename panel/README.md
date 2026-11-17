# PicnicPlace control panel

Firmware for the control panel described in
[docs/hardware/control-panel.md](../docs/hardware/control-panel.md): a touchscreen on the
machine that shows its state and sends the operator's keys to the core, over a serial
link. It keeps no state of its own and never talks to the machine's controller; the core
checks every key, as it checks a click in the Studio.

The reference display is the Waveshare ESP32-S3-Touch-LCD-7B (1024 × 600). The firmware
is written from scratch on ESP-IDF 6 and LVGL 9, under the MPL-2.0, like the rest of
PicnicPlace's software.

## Layout

| Path | What |
|---|---|
| `firmware/` | The ESP-IDF project |
| `firmware/main/board.c` | The board's pins, its RGB screen, its I2C bus, the E-stop input |
| `firmware/main/expander.c` | The board's I/O expander: screen power and reset, backlight, USB/CAN select |
| `firmware/main/gt911.c` | The touch controller |
| `firmware/main/protocol.c` | The JSON messages to and from the core (listed in `protocol.h`) |
| `firmware/main/link.c` | The serial link: native USB, or UART0 for development |
| `firmware/main/ui.c` | The screen |
| `tools/fake_core.py` | A stand-in for the core, to try the panel without a machine |

## Build

The build runs in Espressif's ESP-IDF container, so nothing is installed on the host:

```sh
make panel
```

That builds `panel/firmware/build/picnicplace-panel.bin`. It is not part of `make check`.

## Flash and try

Flash through the board's USB-to-UART port (the CH343 bridge), with esptool on the host
(a container cannot reach a Mac's USB ports):

```sh
cd panel/firmware/build
python -m esptool --chip esp32s3 -p /dev/cu.usbmodem... -b 460800 \
  --before default_reset --after hard_reset write_flash "@flash_args"
```

The panel talks to the core over its **native** USB-C port, which appears as Espressif's
"USB JTAG/serial debug unit"; its log stays on the USB-to-UART port. Run the stand-in core
on the native port, and watch the screen follow it:

```sh
python panel/tools/fake_core.py /dev/cu.usbmodem...   # the native port
```

To develop with one cable, choose UART0 for the link in `idf.py menuconfig` (PicnicPlace
panel → Link to the core); the log then shares the line, and the core ignores lines that
are not JSON.

## Status

Tried on a real 7B (2026-11-17):

- **Working:** the screen, at Waveshare's timing for the 7B; touch; the status bar, the
  position, the core's messages; Home (after the nozzles-level check), Pause or Resume,
  and Stop; "No connection to the core" after two seconds of silence; the backlight's
  brightness; the link over native USB; the E-stop input (it reads pressed with nothing
  wired, as a broken wire should).
- **Learned on the board:** the expander's backlight PWM is inverted (duty 0 is full
  brightness); and the native USB port stays connected to the host whichever way the
  USB/CAN select pin is set.
- **Next:** the core's side of the link (a `[panel]` config section, sending `state` and
  `message`, and taking presses under the control lease); dimming when idle; requests and
  the job's task list on screen.

The board facts (pins, screen timing, the expander's commands) come from Waveshare's
wiki, schematic and published examples; the code is PicnicPlace's own.
