# Send a flight recording

When a pick misses, a fiducial is not found, or a run does something odd, a **flight
recording** lets PicnicPlace's developers see what your machine saw. It keeps:

- every camera frame that vision looked at (homing, fiducials, sprocket holes, parts on
  the nozzles), losslessly, with what vision was asked and what it answered;
- a timeline of what the machine did: moves, vacuum readings, picks, alignments,
  placements, what the bottom camera learned, the core's log, and the run's progress;
- your setup at the start: the config (without its token or data folder), the machine
  profile, the feeders, the parts and footprints in use, and for a run, the job.

Developers use the frames to test vision changes against real images (the core can run
today's vision code on a recording's frames and report every answer that changed), and to
make the simulator more like real machines.

!!! note "What a recording holds"
    A recording holds photos of your boards and, for a run, the job: its parts and where
    they go. Look through it before sending it if your design is private, and send it only
    to people you trust with that.

## Record

- **A run:** in the Run workspace, tick **Record it** before **Start**. The recording
  stops when the run finishes or is stopped. To record every run, set `record_runs = true`
  under `[recorder]` in the config.
- **Anything else** (homing, calibrating a slot, single placements): in the Machine
  workspace's **Flight recorder** panel, type what it is for and click **Record**. Do the
  steps, then click **Stop**.

If the core stops while recording (it crashed, or the computer was turned off), what was
recorded until then is kept: the next time the core starts, the recording is closed and
marked **interrupted**, and can be downloaded like any other. A recording copied out of the
data folder while it is still going says how many frames and events it held when copied.

Only one recording is made at a time. The core keeps the newest ten, and at most 5 GB in
all, and deletes older ones; `keep` and `max_mb` under `[recorder]` change that.

## Send

In the **Flight recorder** panel, click **Zip** beside a recording to download it as one
file. A run of a hundred parts is roughly a few hundred frames: expect hundreds of
megabytes. Recordings are kept in the core's data folder, under `recordings/`, if you would
rather copy them from there.

## For developers: replay a recording

```sh
cd core
uv run picnicd --replay ~/Downloads/20261109-101500-run-picnic-demo.zip
```

It runs today's vision code on every recorded frame and prints each answer that changed,
then a summary such as `37 vision calls: 37 the same, 0 changed`. It exits with status 1
if any changed, so it can run in a test. The format is described in
`core/src/picnicplace/recorder.py`.
