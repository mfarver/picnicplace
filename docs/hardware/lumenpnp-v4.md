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

## Coordinates

+Y points toward the front. The origin is believed to be the right rear corner, so +X
points left as you face the machine. The profile says so (`x_points = "left"`,
`y_points = "front"`), and the Studio draws the map, the jog arrows, and camera images
as an operator facing the machine sees them; vision converts image offsets the same way.

**To confirm:** this conflicts with N1 being the left nozzle. N1's offset from the top
camera is −23.8 mm in X, so if +X points left, N1 is the right-hand nozzle. If jogging
+X moves the head to the right, the origin is the left rear: set `x_points = "right"`.

## Where boards can go

Each nozzle reaches the head's travel shifted by its offset, so with the nozzles 47 mm
apart each reaches 47 mm farther on its own side (N1: X −23.8 … 409.2, N2: X 23.3 …
456.3). The profile computes a placement area where every nozzle can reach, within the
staging plates: X 23.3 … 409.2, Y −19.9 … 213.1. Boards also cannot cover the datum
board, the camera knockout, or the aiming fiducial holder, which are keep-outs in the
profile, so boards go mostly on the rear plate. The holder's size is estimated from a
render (about 40 × 16 mm).

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

Photon feeder positions are not stored in the config; OpenPnP finds them at run time.

The reference machine's discard (157.75, 112.76) and park (389.29, 421.55) positions are
its owner's choices, not part of the design. In PicnicPlace they go in each machine's
config (`[[machine.locations]]`), not the profile.

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

## How PicnicPlace uses this

- The `lumenpnp-v4` profile's `[marlin]` table holds the connect sequence, home command,
  output commands, and vacuum reads above. Its `stock` variant (the default) reads the
  vacuum sensors as one byte; `vacuum-24bit` reads three. Pick one with
  `machine.marlin.variant` in the config.
- The Marlin driver sends one command at a time and waits for its `ok`. For the software
  STOP it sends `M112` immediately, outside the queue; for a quick stop (a controlling
  client went away) `M410`. Both rely on Marlin's emergency parser to act while a move or
  `M400` is in progress; without it they would wait their turn. Whether the LumenPnP
  firmware has it enabled is not known yet.
- After `M112`, resetting the E-stop reconnects and waits up to 30 s for the firmware to
  answer again, asking for the board's reset button if it does not.
- `core/src/picnicplace/machine/marlin/fake.py` is a fake firmware that answers like the
  log above. Run `uv run picnicplace-fake-marlin` for one on a pseudo-terminal.
- Home sensors are not read yet (`M119` replies have not been captured).
- Visual homing is on in the profile: after `G28` the top camera is centered on the
  homing fiducial at (217, 106) and X/Y are corrected with `G92`, as OpenPnP does. The
  profile gives the fiducial's 1.0 mm diameter, and the simulator draws the datum board.
  Until the camera is calibrated, the scale comes from the profile's field of view; the
  Calibration panel's Camera scale button measures the real one.

## Staging plates

From the plate's KiCad design (LumenPnP Staging Plate rev 00): 600 × 120 mm, black.

- M3 holes (3.2 mm) on a 15 mm grid in a checkerboard pattern, columns 1–39 along the
  long side and rows A–G across it (labels on both sides), 133 in all, and four M5
  holes (5.3 mm) near the corners at ±290, ±50 mm from the center.
- A 44 mm round knockout at the plate's center, held by four mouse-bite tabs, to be
  broken out for the bottom camera. The same design is used for both plates; only the
  one over the camera has its knockout removed.
- On the reference machine the camera plate is centered on the bottom camera at
  (217.74, 153.07), so it spans X −82 … 518 and Y 93 … 213.
- The second plate is toward the back (−Y) of the camera plate, knockout left in. The
  profile assumes the two butt together (second plate Y −27 … 93); not measured.
- The datum board stands on a raised spacer just behind the knockout (Y 106) with its
  notched edge toward the camera. A small green board in a clear holder just in front of
  the knockout carries the aiming fiducial, at (217.46, 196.03), Z −31.5, 1.2 mm across.
  With the datum (Z −27.5) it gives the top camera's scale at two heights.

## Datum board (homing fiducial)

From the board's KiCad design (PCB-0004 rev 00, 80 × 35 mm, 1.6 mm). Positions are
relative to the center fiducial, as seen from the front of the machine (+ right, + back);
the board's notched edge, with the ruler, faces the front and the bottom camera.

- **Colors:** LumenPnP boards are black with white silkscreen (the staging plates too).
  The copper finish is not recorded in the design; the simulator assumes gold (ENIG).
- **Homing fiducial (FID1):** a 1.0 mm copper dot. The solder mask opening is 2.0 mm and
  the ground pour is cleared 0.5 mm around the dot, so the dot sits in a ring of bare
  board (no mask, no copper) inside the mask. OpenPnP's calibration measured it as
  0.967 mm at Z −27.5, at (217.21, 106.70), 0.7 mm from the homing location.
- **Around it:** a reticle (a copper ring about 5 mm across with four ticks pointing
  in), an 8 mm copper ring 0.15 mm wide, and a 5 mm grid of 0.15 mm copper lines
  spanning X −10 … +10 and Y −10 … +5.
- **Other fiducials:** 1.0 mm dots in 3 mm openings at (±10, +5) and (±10, −10), the grid
  corners, and at (±35, −20).
- **Also on the board:** a 30 mm ruler with 1 mm ticks along the front edge (Y −15), a
  5 mm exposed copper disc at (−22.5, −7.5), four plated M3 mounting holes, and on the
  back a mask-defined dot grid (likely for bottom camera calibration).
- **For detection:** the board has three brightness levels around the dot (copper, bare
  board, mask), so a single threshold merges the dot with its 2 mm window. The finder
  sweeps thresholds and keeps blobs that stay round and 0.6–1.5 mm across many levels.
  It only accepts a fiducial within 4 mm of the expected position; the nearest other
  fiducial is more than 11 mm away, so it cannot lock onto the wrong one.
- **Useful later:** the grid corners (20 × 15 mm) and the ruler are known distances for
  checking camera scale, rotation, and X/Y squareness.

## Cameras

Two USB (UVC) cameras named "LumenPnP Top" and "LumenPnP Bottom", 1920 × 1080. OpenPnP
on the reference machine read them as uncompressed YUYV through V4L2, ran the top camera at
5 frames per second, and flipped both images (top: X and Y, bottom: X). PicnicPlace asks
for MJPEG first, since USB 2 cannot carry uncompressed 1080p quickly; whether these
cameras offer MJPEG is not known yet (`v4l2-ctl --list-formats-ext -d /dev/videoN` shows
it). Image flips are not applied yet.

## Bringing up a machine

`picnicplace-probe --serial-port /dev/ttyACM0 --with-pumps` collects what is missing
below without moving the machine: `M115` with capabilities (including the emergency
parser), `M119`, `M503`, `M114`, vacuum reads in both firmware variants at rest and with
suction on, and the cameras with their formats. It saves everything to a transcript.

## Not seen in the log

- `M112` (emergency stop) and how the board recovers from it.
- `M119` (endstop states) and `M503` (stored settings).
- Replies from stock firmware.
