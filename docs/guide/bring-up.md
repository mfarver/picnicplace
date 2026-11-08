# Bring up a machine

!!! warning "Not yet tested on a real machine"

    These steps have been run against the fake firmware only. The first real LumenPnP
    bring-up will refine them. Keep a hand on the machine's physical E-stop throughout.

PicnicPlace runs on Linux for real machines. These steps are for a LumenPnP v4.1 with
stock firmware; other machines need their own machine profile.

## 1. Probe the firmware

`picnicplace-probe` asks the firmware about itself and never sends a motion command:

```sh
cd core
uv run picnicplace-probe --serial-port /dev/ttyACM0 --with-pumps
```

It reports:

- the firmware's name and version, and whether STOP can act in the middle of a move
  (Marlin's emergency parser)
- endstop states, stored settings, and the position the firmware reports
- vacuum readings in each firmware variant the profile knows, to show which one your
  firmware speaks
- with `--with-pumps`, each nozzle's vacuum with its pump and valve on for about a second
- the cameras and their video formats

It saves everything to a transcript file. Include that file when asking for help with a
machine.

## 2. Configure the core

Copy [`core/picnicd.example.toml`](https://github.com/mfarver/picnicplace/blob/main/core/picnicd.example.toml)
and set at least:

```toml
[machine]
driver = "marlin"
profile = "lumenpnp-v4.1"
speed_scale = 0.25         # quarter speed while you check the machine

[machine.marlin]
port = "/dev/ttyACM0"
```

If the probe showed vacuum readings only in the `vacuum-24bit` variant, also set
`variant = "vacuum-24bit"` under `[machine.marlin]`.

## 3. First motion

Start the core with your config and open the Studio, then work through this checklist:

- [ ] Keep a hand on the machine's physical E-stop.
- [ ] Check the Studio shows the "Slow 25%" badge.
- [ ] Home, and watch that every axis reaches its switch. After homing, Z should read 0.
- [ ] Jog X and Y by 1 mm, then 10 mm. Check the head moves the way the arrows point.
- [ ] Jog each nozzle down 1 mm and back up. Check N1 and N2 move the right way.
- [ ] Turn each light, pump, and valve on and off from the Head inset.
- [ ] Press STOP during a slow 50 mm move. Note whether it stops at once, and what it
      takes to recover: Reset in the Studio, the board's reset button, or a power cycle.

## 4. Cameras

Add the cameras to your config (see the `[cameras]` section of the example), using the
stable paths under `/dev/v4l/by-id/` that the probe listed. Then check:

- [ ] Jog right: the top camera's picture slides left. Jog back: it slides down. If not,
      adjust `flip_x`, `flip_y`, or `rotation`.
- [ ] **Visual home** in the Calibration panel finds the datum fiducial and reports a
      correction of well under a millimeter.
- [ ] **Camera scale** reports a size close to the profile's field of view.

## 5. Full speed

When everything above checks out, remove `speed_scale` (or set it to 1.0) and restart
the core.
