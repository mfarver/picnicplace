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

Start the core with your config and open the Studio. The **Bring-up** panel in the
Machine workspace walks through the steps below and the camera and feeder checks after
them. It keeps your progress, notes, and findings in the core's data directory, and
turns findings that differ from the machine profile (which way an axis points, measured
keep-outs) into TOML you can copy into a profile. The same steps:

- [ ] Keep a hand on the machine's physical E-stop.
- [ ] Check the Studio shows the "Slow 25%" badge.
- [ ] Home, and watch that every axis reaches its switch. After homing, Z should read 0.
- [ ] Jog X and Y by 1 mm, then 10 mm. Check the head moves the way the arrows point:
      +X to the right and +Y toward the back on a LumenPnP v4.1.
- [ ] Jog Z down 1 mm and back up. The left nozzle (N1) should go down.
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

## 5. Feeders

- [ ] **Scan feeders** in the Feeders workspace. Each feeder found is initialized and
      listed under its slot.
- [ ] Give each feeder its part, and feed one. Check it moves one part's pitch.
- [ ] On one slot, center the camera over the pocket and click **Pick is here**. Then
      center it on the feeder's alignment fiducial and click **Measure fiducial here**.
- [ ] **Calibrate all slots**: the camera visits every feeder's fiducial and corrects
      each slot's pick location.
- [ ] Feed a few times, then **Find pocket**. It finds the sprocket holes and centers the
      camera on the next part's pocket.

## 6. One part

- [ ] Give a feeder's part a height in the Parts workspace, and put a scrap board on the
      staging plate.
- [ ] In the Machine workspace's **Single placement** panel, choose the nozzle and the
      feeder, then **Pick**. It feeds, finds the pocket, picks at the tape's part height,
      lifts, and checks the vacuum says a part is held.
- [ ] **Align**: the part is shown to the bottom camera with its underside in focus, and
      the panel reports how far off center and how turned it sits on the nozzle.
- [ ] Center the top camera on a spot on the board, click the crosshair button to use
      its position, and **Place**. The part is let go there, and the nozzle is checked
      to be empty after lifting.
- [ ] Use the **Capture** panel on the way: the pocket from above, the part on the
      nozzle from below, and the placed part. Captured frames are saved losslessly in the
      data directory's `captures/` folder, for tuning vision on real images.

## 7. A whole job

- [ ] In the Prepare workspace, import the board (and its panel), use **Find a place**,
      fasten the board where the map shows it, and save the job. Its parts may run to
      the edges of where both nozzles reach; rails without parts may go beyond.
- [ ] In the Run workspace, choose the job. The checks are a dry run against the loaded
      feeders: each placement's feeder and nozzles, and what blocks any of them (no part
      number, no height, no feeder holding the part, tape too wide for the feeders, or
      out of reach). Fix what you can; tick **Place the rest** to leave out the others.
- [ ] **Start**, then **Pause** at once: the run finds the board's fiducials and pauses
      before the first part. Check the reported offset and turn look right.
- [ ] **Step** places one part on each nozzle and pauses again. Check them, then
      **Resume**. A jammed feeder or a failed pick pauses the run with the parts already
      on the nozzles kept there; fix it and resume, or skip that placement.

## 8. Measure what the profile only estimates

With the camera centered on each point, record it in the checklist's last steps: the
corners of the datum board, the camera opening, the aiming fiducial's holder, and the
rear staging plate. Points that share a first word become a keep-out rectangle in the
suggested changes.

## 9. Full speed

When everything above checks out, remove `speed_scale` (or set it to 1.0) and restart
the core.
