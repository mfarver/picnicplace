# LumenPnP v4: reference notes

What PicnicPlace knows about the LumenPnP v4 and how its firmware talks. Gathered from a
working machine's OpenPnP configuration (`machine.xml`) and a TRACE-level OpenPnP log
(OpenPnP 2.7, November 2026), shared by a LumenPnP owner. The raw files are not kept in
this repository; this page records the facts PicnicPlace relies on.

Items marked **inferred** are reasoned from the config rather than observed directly and
need checking on a machine.

## Versions

PicnicPlace's `lumenpnp-v4.1` profile is for **v4.1 and earlier v4** machines: PCB
staging plates with the bottom camera in a knockout at the center of the front plate.
The project lead's machine and the reference machine are both v4.1.

**v4.2** switched to laser-cut metal staging plates (3 mm) and moved the camera. From
Opulo's repository (OpenPnP configuration of October 2026 and the plate design), for a
future `lumenpnp-v4.2` profile:

- The camera plate is the same 600 × 120 mm size and position, but its knockout is
  60 mm across and 120 mm from the plate's −X end. Bottom camera at (38.38, 154.63),
  homing fiducial at (37.37, 110.04).
- The second calibration fiducial (the separate cal-fid board in its own holder) is at
  (63.74, 109.89), level with the datum.
- Nozzle offsets N1 (−23.54, −65.19), N2 (23.92, −64.39).
- The plates' hole pattern is a 30 mm grid (not fully extracted; FreeCAD source only).

**Also confirmed by Opulo's configuration:** the stock firmware reads the vacuum sensors
as one byte (`M261 A109 B1 S2`, "request one byte back via decimal"); the connect
sequence, outputs, and lights match v4.1; and OpenPnP's Z runs 0 to 63 with both nozzles
level at 31.5 (N2 mapped to 63 − Z). Firmware builds differ in what they report after
`G28`: the reference machine's log shows Z = 0, and another v4.1 (Marlin bugfix-2.1.x,
September 2024) reports Z = 31.5. PicnicPlace calls level Z = 0, so the profile follows
`G28` with `G92 Z0`, which is right for both. The repository does not contain the
firmware.

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
- Z after homing: `G28` lowers N1 until N2 reaches the Z switch, then brings both nozzles
  back to level. Some firmware reports that as Z = 0 (the reference machine's log), some
  as **Z = 31.5** (a v4.1 with the September 2024 bugfix-2.1.x build). The profile sends
  `G92 Z0` after `G28` so it reads 0 either way. Without that, on the 31.5 firmware every
  height is 31.5 mm off and the lift to safe Z drives N1 down: on a real v4.1 this
  dragged N1 into the feeders. The
  core now checks Z after homing against the profile's `home_z` (0) and does not call
  the machine homed if it differs.
- After `G28`, OpenPnP homes visually: it moves the top camera over the homing fiducial
  at (217, 106), finds it, and corrects the coordinates with `G92`.
- Move: `M204 T<accel>` then `G0 X… Y… Z… A… B… F<feed>`. Feed is in mm/min (OpenPnP sends
  up to about `F38000`). Only axes that move are sent.
- Wait for motion to finish: `M400`, which answers `ok` once moves complete.
- Position: `M114` answers
  `X:0.00 Y:0.00 Z:0.00 A:-6300.00 B:-540.00 Count X:0 Y:0 Z:0 A:-27972 B:-2398`.
- Dwell: `G4 P<ms>`.

## Coordinates

+X points right and +Y toward the back, as you face the machine; the origin is the front
left corner. The profile says so (`x_points = "right"`, `y_points = "back"`), and the
Studio draws the map (with the front marked), the jog arrows, and camera images as an
operator facing the machine sees them; vision converts image offsets the same way.

Earlier versions of the profile had both axes the other way (+X left, +Y front). Three
things say this way is right: the bottom camera (Y 153 of 0 … 487) is near the front of
the machine, N1 (at −23.8 mm X from the camera) is then the left nozzle as OpenPnP names
it, and the front feeder rail (pick Y ≈ 73) no longer lands on a staging plate.
**To confirm** by jogging: +X should move the head right, +Y toward the back. The
bring-up checklist in the Studio asks.

Opulo's assembly model (a STEP file from April 2022, so an earlier LumenPnP than v4.1)
agrees. Placed by its bottom camera, it has the nozzles 66 mm in front of the top camera
and 24 mm to each side (OpenPnP's offsets are 64.4 and 23.3 to 23.8), the Y endstop at
the front left, the X endstop at the left, the 600 × 600 mm frame at X −82 … 518, and
its one staging plate where the profile puts the camera plate. Front to back it does not
fit v4.1: it would put the frame's front at Y −31, but at Y home (0) the nozzles are at
Y −64. So v4.1's bottom camera sits farther back in the frame than the model's, and the
profile's bed outline (Y −75 … 525) is set to clear the nozzles at home instead. Its
datum board is behind the camera (Y ≈ 191), where v4.1 has the aiming fiducial; the
datum, the plates, and the camera have all moved since, and the model has no feeder
rails.

## Where boards can go

Each nozzle reaches the head's travel shifted by its offset, so with the nozzles 47 mm
apart each reaches 47 mm farther on its own side (N1: X −23.8 … 409.2, N2: X 23.3 …
456.3). The profile computes a placement area where every nozzle can reach, within the
staging plates: X 23.3 … 409.2, Y 93.1 … 333.1. Boards also cannot cover the datum
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

OpenPnP keeps each Photon slot's taught pick location in `machine.xml`. On the reference
machine the front rail's slots (addresses 1–25) pick at Y ≈ 73.2 from X 28.6 every
15.77 mm, and the back rail's (26–50) at Y ≈ 388.6 from X 405.1 every −15.76 mm; single
slots scatter about 1 mm around those lines. LumenPnP owners number the slots the same way, 1–25 along
the front rail from left to right and 26–50 along the back rail from right to left, and
PicnicPlace names slots by those numbers.

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
`rs485-reply: <hex packet>` or `rs485-reply: TIMEOUT` (about 100 ms). The packets are
Opulo's Photon protocol, described at docs.opulo.io. PicnicPlace's implementation
(`core/src/picnicplace/feeders/photon.py`) was written from that description and decodes
every packet in the reference machine's log with a valid checksum.

- **Header:** to, from, packet ID, payload length, then a CRC-8 (polynomial 0x07,
  initial 0) of those four bytes and the payload. The host is address 0, broadcast 255.
  A reply repeats the request's packet ID.
- **Addresses** belong to slots, not feeders: each slot's address is programmed into
  the rail (the "feeder floor"). Every feeder also has a fixed 12-byte ID.
- **Commands:** get feeder ID (0x01), initialize (0x02, with the ID; a feeder only feeds
  once initialized), version (0x03), feed forward and back (0x04, 0x05, in tenths of a
  millimeter, at most 25.5 mm per command), feed status (0x06), and broadcasts to find
  (0xC0), identify (0xC1, flashes its light), and program a slot's address (0xC2).
- **Feeding blocks:** the feeder answers the feed command with the time it expects to
  take, ignores the bus until done, and then answers a status poll.
- The reference machine runs modified feeder firmware and a modified OpenPnP that feed
  with commands 0x0A and 0x0B and report extra diagnostics. Stock OpenPnP and stock
  feeders use 0x04 and 0x06, which is what PicnicPlace sends.

From Opulo's feeder design (the 8 mm assembly; the 12 mm one shares the nose): the frame
is 137 mm long and 15 mm wide, and its pick window is 12.75 mm long, open over both the
pockets and the sprocket holes. The profile's pick location is the window's middle
pocket; the feeder reaches 27.3 mm past it, so on the front rail the noses reach over
the camera plate's front edge, and boards are kept clear of them. A 4 × 12 mm board on
the nose carries a 1.0 mm fiducial in a 2 mm opening, 19.15 mm along the feed from the
pick location and 4.1 mm from the sprocket holes' line toward the pockets (so 0.6 mm
past the pocket line on 8 mm tape, and 1.4 mm short of it on 12 mm), with 0.5 mm marks
2 mm to either side whose spacing encodes the feeder's width. Opulo's design notes say
the board's placement varies a little from feeder to feeder.

Photon feeders have an alignment fiducial. PicnicPlace starts from the design's offset,
can measure it from the pick location once (on the sprocket-hole line, so the same for
every tape width), and then finds every slot's pick location from its feeder's fiducial. At pick
time it can also find the pocket from the tape's sprocket holes, since feeders do not
step exactly and their errors add up (EIA-481 puts each pocket 2 mm along the tape from
a hole, and a fixed distance across it).

## How PicnicPlace uses this

- The `lumenpnp-v4.1` profile's `[marlin]` table holds the connect sequence, home command,
  output commands, and vacuum reads above. Its `stock` variant (the default) reads the
  vacuum sensors as one byte; `vacuum-24bit` reads three. Pick one with
  `machine.marlin.variant` in the config.
- The Marlin driver sends one command at a time and waits for its `ok`. For the software
  STOP it sends `M112` immediately, outside the queue; for a quick stop (a controlling
  client went away) `M410`. Both rely on Marlin's emergency parser to act while a move or
  `M400` is in progress; without it they would wait their turn. Whether the LumenPnP
  firmware has it enabled is not known yet.
- After `M112`, resetting the E-stop reconnects and waits up to 30 s for the firmware to
  answer again, asking for the board's reset button if it does not. A halted controller
  stops reading, and closing its port then waits for unsent output to drain (up to 30 s
  on Linux): on a real v4.1 that froze the core for about a minute. The core now drops
  unsent output and closes the port in the background, so it stays responsive.
- `core/src/picnicplace/machine/marlin/fake.py` is a fake firmware that answers like the
  log above, including Z = 31.5 after `G28` and `G92` renaming positions. Run `uv run picnicplace-fake-marlin` for one on a pseudo-terminal.
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
- The second plate is behind (+Y) the camera plate, knockout left in. The profile
  assumes the two butt together (second plate Y 213 … 333); not measured.
- The datum board stands on a raised spacer just in front of the knockout (Y 106) with
  its notched edge toward the camera. A small green board in a clear holder just behind
  the knockout carries the aiming fiducial, at (217.46, 196.03), Z −31.5, 1.2 mm across.
  With the datum (Z −27.5) it gives the top camera's scale at two heights.

## Datum board (homing fiducial)

From the board's KiCad design (PCB-0004 rev 00, 80 × 35 mm, 1.6 mm). Positions are
relative to the center fiducial, in the board's own frame with the notched edge (the
ruler side) at −Y; the board is mounted with that edge facing the bottom camera.

- **Colors:** LumenPnP boards are black with white silkscreen (the staging plates too).
  The copper finish is not recorded in the design; the simulator assumes gold (ENIG).
- **Homing fiducial (FID1):** a 1.0 mm copper dot. The solder mask opening is 2.0 mm and
  the ground pour is cleared 0.5 mm around the dot, so the dot sits in a ring of bare
  board (no mask, no copper) inside the mask. OpenPnP's calibration measured it as
  0.967 mm at Z −27.5, at (217.21, 106.70), 0.7 mm from the homing location.
- **Around it:** a reticle (a copper ring about 5 mm across with four ticks pointing
  in), an 8 mm copper ring 0.15 mm wide, and a 5 mm grid of 0.15 mm copper lines
  spanning X −10 … +10 and Y −10 … +5.
- **Not on this board:** the aiming fiducial beyond the camera is a separate
  20 × 20 mm board (cal-fid) in its own holder, with the same 1.0 mm dot in a 2 mm opening.
- **Other fiducials:** 1.0 mm dots in 3 mm openings at (±10, +5) and (±10, −10), the grid
  corners, and at (±35, −20).
- **Also on the board:** a 30 mm ruler with 1 mm ticks along the notched edge (Y −15), a
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
it). PicnicPlace flips images as the config says (`flip_x`, `flip_y`): a real v4.1
needed both flips for the top camera, the same as OpenPnP's, and `flip_y` for the bottom
camera (not yet checked), so the images look like the machine seen from the front.

## Bringing up a machine

`picnicplace-probe --serial-port /dev/ttyACM0 --with-pumps` collects what is missing
below without moving the machine: `M115` with capabilities (including the emergency
parser), `M119`, `M503`, `M114`, vacuum reads in both firmware variants at rest and with
suction on, and the cameras with their formats. It saves everything to a transcript.

## Not seen in the log

- `M112` (emergency stop) and how the board recovers from it.
- `M119` (endstop states) and `M503` (stored settings).
- Replies from stock firmware.
