# LumenPnP v4: reference notes

What PicnicPlace knows about the LumenPnP v4 and how its firmware talks. Gathered from a
working machine's OpenPnP configuration (`machine.xml`) and a TRACE-level OpenPnP log
(OpenPnP 2.7, November 2026), shared by a LumenPnP owner. The raw files are not kept in
this repository; this page records the facts PicnicPlace relies on.

Items marked **inferred** are reasoned from the config rather than observed directly and
need checking on a machine.

## Firmware

- Marlin `bugfix-2.1.x`, built Sep 25 2026, `MACHINE_TYPE:LumenPnP`, `AXIS_COUNT:5`
  (X, Y, Z, A, B), Cartesian. Reported by `M115`.
- Serial over USB (`/dev/ttyACM0`), 115200 baud, 8N1, no flow control, LF line endings.
- Every command is answered with `ok`. OpenPnP waits for `ok` before counting a command
  as confirmed (`^ok.*`).
- During `G4` dwells the firmware sends `//action:notification Sleep...` before `ok`.
  Lines starting with `//` or `echo:` are informational.
- **Firmware variants.** The reference machine runs a patched firmware whose vacuum sensor
  read returns a signed 24-bit value. Stock Opulo firmware returns 8 bits (see Vacuum).

## Connect sequence

What OpenPnP sends after opening the port (and waiting 3 s for the board to reset):

| Command | Purpose |
|---|---|
| `G90` | Absolute positioning |
| `M260 A112 B1 S1`, then `M260 A109`, `M260 B48`, `M260 B27`, `M260 S1` | Configure vacuum sensor 1: select it through the I2C multiplexer (address 112, channel 1), then write `0x1B` to the sensor's command register (address 109, register 48): sleep-mode conversions every 62.5 ms |
| The same with `M260 A112 B2 S1` | Configure vacuum sensor 2 |
| `M113 S0` | Disable host keepalive messages |
| `M203 X2000 Y2000 Z600 A60000 B60000` | Raise maximum feed rates |
| `M201 X20000 Y20000 Z15000 A30000 B30000` | Raise maximum accelerations |
| `M205 J0.02` | Junction deviation |

## Motion

- Home: `G28` homes X, Y, and Z (about 5 s) and reports the new position before `ok`.
  **A and B are not homed** and keep their positions (for example `A:-6300.00`).
- After `G28`, OpenPnP homes visually: it moves the top camera over the homing fiducial
  at (217, 106), finds it, and corrects the coordinates with `G92`.
- Move: `M204 T<accel>` then `G0 X… Y… Z… A… B… F<feed>`. Feed is in mm/min (OpenPnP sends
  up to about `F38000`). Only axes that move are sent.
- Wait for motion to finish: `M400`, which answers `ok` once moves complete.
- Position: `M114` answers
  `X:0.00 Y:0.00 Z:0.00 A:-6300.00 B:-540.00 Count X:0 Y:0 Z:0 A:-27972 B:-2398`.
- Dwell: `G4 P<ms>`.

## Axes and geometry

From the reference machine (calibrated values vary slightly between machines):

| Item | Value |
|---|---|
| X soft limits | 0 … 433 mm, 600 mm/s, 3000 mm/s² |
| Y soft limits | 44.454 … 487 mm (homes at 0, below the lower limit), 500 mm/s |
| Z | One motor drives both nozzles in opposite directions: N2's Z is −Z, over ±28.5 mm. 400 mm/s, 7000 mm/s² |
| Safe Z | OpenPnP treats Z within −15 … +15 as safe for travel |
| A, B | Nozzle rotation, 720 °/s, continuous (wrap-around) |
| Board height | About Z −27.5 (camera focus plane), trays −20 … −29 |
| Nozzle offsets from the top camera | N1 (−23.84, −64.41), N2 (+23.34, −64.38) |
| Top camera | 1920 × 1080, 0.01075 mm/pixel at board height (about 20.6 × 11.7 mm) |
| Bottom camera | At (217.74, 153.07), 1920 × 1080, 0.0206 mm/pixel (about 39.6 × 22.3 mm) |
| Homing fiducial | (217, 106) |
| Discard | (157.75, 112.76) |
| Park | (389.29, 421.55) |

Photon feeder positions are not stored in the config; OpenPnP finds them at run time.

## Vacuum

Each nozzle has a pump and a valve on Marlin fan outputs, switched together by OpenPnP:

| Nozzle | On | Off |
|---|---|---|
| N1 | `M106 P1 S180` and `M106` (P0, full) | `M107 P1` and `M107` |
| N2 | `M106 P2 S255` and `M106 P3 S180` | `M107 P2` and `M107 P3` |

**Inferred:** the outputs at PWM 180 (P1, P3) are the pumps and the full-on ones (P0, P2)
are the valves.

Reading a sensor (patched 24-bit firmware):

```
M260 A112 B1 S1     ; select sensor 1 (B2 for sensor 2) through the multiplexer
M260 A109 B6 S1     ; point at the pressure register (6)
M261 A109 B3 S4     ; read 3 bytes
echo:i2c-reply: from:109 bytes:3 data:-1497247
ok
```

- `data:` is a signed 24-bit count. It reads near 0 at rest and grows **more negative**
  with more vacuum.
- With suction on and the nozzle open, N1 settles near −1,500,000 within about 0.5 s.
- The nozzle tip settings treat −1,600,000 and below as part held, and −1,600,000 to
  +200,000 as no part.
- N2 on the reference machine only reached about −500,000 during the session (N2 probing
  was off in its config); worth checking on another machine.
- **Stock firmware** reads one byte (`M261 A109 B1 S2`). **Inferred:** that is the top byte
  of the same register, so the driver scales it by 2¹⁶ to report the same counts. This
  needs a capture from stock firmware to confirm, including whether the byte is signed.

OpenPnP polls the sensor continuously while waiting for vacuum (every 3–4 ms).

## Lights

NeoPixel rings via `M150`: `M150 P255 R255 U255 B255 S1` turns the top ring on, `S0` the
bottom ring; `M150 P0 S1` / `M150 P0 S0` turn them off.

## Photon feeders

RS-485 through the motherboard: `M485 <hex packet>`, answered with
`rs485-reply: <hex packet>` or `rs485-reply: TIMEOUT`. The packet format is Opulo's Photon
protocol and is not described here yet.

## Not seen in the log

- `M112` (emergency stop) and how the board recovers from it.
- `M119` (endstop states) and `M503` (stored settings).
- Replies from stock firmware.
