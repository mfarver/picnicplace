# Bring up a machine

!!! warning "Tested on one real machine so far"

    These steps have been run against the fake firmware and one real LumenPnP v4.1
    (homing, jogging, and board fiducials), and will be refined as more machines are
    brought up. The LumenPnP has no E-stop: keep a hand on its power switch throughout.
    STOP in the Studio waits for the current move to finish on its stock firmware.

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

Start the core with the config: `uv run picnicd -c path/to/picnicd.toml`. Without `-c` it
runs the built-in simulator instead: it says so when it starts, and the Studio's status
bar shows **Simulator**.

## 3. First motion

Start the core with your config and open the Studio. The **Bring-up** panel in the
Machine workspace walks through the steps below and the camera and feeder checks after
them. It keeps your progress, notes, and findings in the core's data directory, and
turns findings that differ from the machine profile (which way an axis points, measured
keep-outs) into TOML you can copy into a profile. The same steps:

- [ ] Keep a hand on the machine's power switch (or its E-stop, if it has one).
- [ ] Check the Studio shows the "Slow 25%" badge.
- [ ] Home, and watch that every axis reaches its switch. After homing, Z should read 0
      with both nozzles level. If the firmware reports anything else, homing fails with
      a message saying so: the profile's home commands need a `G92` (the LumenPnP
      profile sends `G92 Z0`, because its firmware calls level 31.5).
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
- [ ] **Calibrate rails** (under **Rails**): with feeders near both ends of each rail (the
      core uses the two farthest apart), it finds their fiducials and moves the rail's
      slots to where the rail really is. If it cannot find one, jog the camera onto that
      feeder's fiducial, select its slot, and click **Rail fiducial is here**.
- [ ] On one slot, center the camera over the pocket and click **Pick is here**. Then
      center it on the feeder's alignment fiducial and click **Measure fiducial here**.
- [ ] **Calibrate all slots**: the camera visits every feeder's fiducial and corrects
      each slot's pick location. This is optional before a run: a run finds each
      feeder's fiducial on its first pick anyway. A correction belongs to the feeder it
      was found from; put another feeder in the slot and it is found again.
- [ ] Feed a few times, then **Find pocket**. It finds the sprocket holes and centers the
      camera on the next part's pocket.
- [ ] For parts held on the bed (a cut strip of 24 mm tape in a holder, or a tray), use
      **Add strip or tray** under **Strips and trays**. Give it its part, the number of
      parts (and rows, for a tray), and a pick height: the nozzle height at the top of a
      part, measured with the nozzle jogged down onto one. Center the camera on the first
      part and click **First part**, then on the last part and click **Last part** (on a
      tray, also the end of the first row). On a strip, **Find pocket** should land on
      the next part. The map shows how many parts each one has left.

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
- [ ] **Start**, then **Pause** at once: the run scans the feeders (a feeder moved
      since the checks is picked from where it is now), finds the board's fiducials, and
      pauses before the first part. Check the reported offset and turn look right.
      Before the first pick from each feeder, the camera visits that feeder's fiducial.
- [ ] **Step** places one part on each nozzle and pauses again. Check them, then
      **Resume**. A jammed feeder or a failed pick pauses the run with the parts already
      on the nozzles kept there; fix it and resume, or skip that placement. A part the
      bottom camera cannot find goes to the discard location; without one, the run moves
      the head out of the way and pauses: take the part off the nozzle, click **Part
      removed**, and resume to pick it again.
- [ ] With a part on a strip or tray, the run takes them in order and pauses when one is
      empty, with the nozzles up and the head moved out of the way: to the park
      location, if `[[machine.locations]]` in the config has one (`kind = "park"`, its
      position the head's X and Y), or else to the back of its travel. Refill it, click
      **Refilled** (in the Run workspace's **Strips and trays** panel), and resume.
- [ ] Compare the two ways of keeping picks centered. **Keep picks centered with** in
      the Run workspace offers the top camera (find each pocket from the sprocket holes
      before the pick) or the bottom camera (move each feeder's picks by how far off
      center it saw that feeder's earlier parts, with no trip to the pocket first). Run
      the job once each way. Each feeder's detail in the Feeders workspace shows how far
      off the nozzle's center its parts sat, on average, for each way. Set the one you
      prefer as the default with `pick_correction` under `[machine]` in the config.
      **Auto** combines them: the top camera on each feeder's first pick in a run and
      after a bad pick, the bottom camera otherwise. Each pick's message says when auto
      chose the top camera, and why.

## 8. Measure what the profile only estimates

With the camera centered on each point, record it in the checklist's last steps: the
corners of the datum board, the camera opening, the aiming fiducial's holder, and the
rear staging plate. Points that share a first word become a keep-out rectangle in the
suggested changes.

## 9. Full speed

When everything above checks out, remove `speed_scale` (or set it to 1.0) and restart
the core.
