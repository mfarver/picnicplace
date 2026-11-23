# Network API

Everything PicnicPlace Studio does goes through the same public API, so anything the
Studio can do, your own scripts and interfaces can do too. The design is in
[§6 of the specification](../SPEC.md#6-network-api).

!!! note "Version 1, still changing"

    The API lives under `/api/v1`. Until PicnicPlace 1.0 it may still change between
    releases; after 1.0, breaking changes will need a new version.

## Reference

The [OpenAPI 3.1 document](openapi.json) describes every endpoint and message, including
the WebSocket event messages. It is generated from the core's code, and CI checks it is up
to date, so it always matches the code on `main`. Load it into any OpenAPI viewer, or
generate a client from it, as the Studio does.

## Channels

| Channel | Use |
|---|---|
| REST, JSON over HTTP | Machine description and state, commands, cameras, the control lease |
| WebSocket at `/api/v1/events` | Live state, operation progress, and presence, and `job.changed` when a saved job, its revisions, or where this machine has it set up changes (clients showing the job fetch it again). A client must hold this connection to move the machine. |
| MJPEG over HTTP | Camera streams and snapshots at the size, rate, and quality the client asks for |

## Endpoints today

| Method and path | What it does |
|---|---|
| `GET /api/v1/health` | Is the core running |
| `GET /api/v1/machine` | Live machine state: position, inputs, outputs, safe Z, E-stop |
| `GET /api/v1/machine/description` | The machine profile: axes, nozzles, cameras, plates, I/O |
| `GET`, `PUT /api/v1/machine/local-profile` | This machine's local profile; save bring-up findings to it (they take effect on restart) |
| `PUT`, `DELETE /api/v1/machine/locations/{kind}` | Set the discard, park or paper pad location (by default where the top camera looks), or reset it |
| `POST /api/v1/machine/connect` | Connect to the machine's controller |
| `POST /api/v1/machine/home` | Home, then visual home if the profile enables it |
| `POST /api/v1/machine/move` | Move to a position, traveling at safe Z |
| `POST /api/v1/machine/park` | Raise the nozzles and move the head to the park location (else the back) |
| `POST /api/v1/machine/jog` | Jog one axis by a distance |
| `PUT /api/v1/machine/outputs/{id}` | Switch a pump, valve, or light |
| `PUT /api/v1/machine/lights/{id}` | Set a light's color and brightness (each 0 to 255; brightness 0 is off), if the profile marks it colored |
| `POST /api/v1/machine/visual-home` | Center the top camera on the homing fiducial and correct the position |
| `POST /api/v1/machine/camera-scale` | Measure the top camera's scale, kept and used by vision |
| `POST /api/v1/machine/bottom-camera` | Calibrate the bottom camera with an empty nozzle tip: its place, focus height, and scale, kept and used |
| `POST /api/v1/machine/nozzle-offsets` | Calibrate the nozzle offsets with a small disc on the datum board, the top camera centered on it: each nozzle sniffs for the disc's top (the board's height is that less its thickness), then turns the disc. Its vacuum levels (open, sealed, holding the disc) are measured on the way, setting its own pick threshold. A tip too big to seal on the disc is changed first: the machine asks for one, with the head at the front. Kept and used |
| `PUT /api/v1/machine/nozzles/{nozzle_id}/tip` | Say which of the profile's nozzle tips is now fitted on a nozzle (`tip_id`, or null), after changing it by hand. Kept; the nozzle's Z offset and vacuum levels become those measured with that tip. Refused while the nozzle has suction on. Returns the machine profile |
| `GET /api/v1/calibration` | The calibration checklist: every calibration the machine needs, in order, each done (with its value and when), to do, out of date (and why), or from the profile |
| `GET /api/v1/wizard` | The setup wizard: every page in order with where it stands, the machine's stage (`limited`, `out_of_date`, `calibrated`), the page Continue opens, and whether the limits are enforced. Until calibrated, picks, placements and runs are refused with code `calibration_required` |
| `POST /api/v1/wizard/run` | Run a stop of the wizard (`stop`: 1, with a hand on the power switch: homing, visual homing, both cameras, naming the tips, the feeders; 2, with a paper disc on the datum board: each nozzle's offset, height and vacuum) as one operation, stopping at the first step that fails. Progress is in the wizard's `run`. Needs control |
| `PUT /api/v1/wizard/pages/{page_id}` | Confirm a page the owner finishes by hand (`discard_park`), skip an optional one (`strips_trays`), or open it again (`status`: `done`, `skipped`, `todo`). Needs control |
| `GET /api/v1/setup` | The setup wizard's choices (machine, driver, serial port, cameras, last probe), what the core is running, what the config file pins, the built-in machines, and whether a restart is needed |
| `PUT /api/v1/setup` | Keep the wizard's choices; used when the core next starts. Needs control |
| `GET /api/v1/setup/serial-ports`, `GET /api/v1/setup/cameras` | The serial ports and cameras the wizard offers, found without opening them |
| `POST /api/v1/setup/probe` | Ask a serial port's firmware what it is and read its vacuum sensors; nothing moves or switches on. Kept with the choices. Needs control |
| `POST /api/v1/setup/restart` | Restart the core to use the wizard's choices (when `picnicd` started it). Needs control |
| `POST /api/v1/machine/pause` | Hold the machine before its next move or feeder advance (the move under way finishes); nothing new starts; any client may call it |
| `POST /api/v1/machine/resume` | Carry on after a pause |
| `POST /api/v1/machine/nozzle-runout` | Measure how far the tip's end on some nozzles (`nozzle_ids`: those whose tip has none yet, unless given) is from the axis it turns about: each turns over the bottom camera through eight angles, and a circle is fitted to its end. Picks then put the end, not the axis, over the part. Kept for the tip fitted, and used. Needs control |
| `POST /api/v1/machine/tip-heights` | Measure the height and vacuum levels of the tip on some nozzles (`nozzle_ids`: those whose tip has none yet, unless given) on a square of plain paper on the datum board, found by the top camera. For tips too big for the calibration disc, once the nozzle offsets are measured. Kept and used. Needs control |
| `POST /api/v1/machine/tip-change` | Raise the nozzles, bring the head to the front and center, and ask the operator (the machine state's `request`) to change tips by hand and say which tip is on each nozzle (`nozzle_ids`: all, unless given); then touch the paper pad with each tip, if one is set, to check it sits as when its height was measured, and, once the bottom camera is calibrated, measure the runout of each tip just fitted. Needs control |
| `POST /api/v1/machine/answer` | Answer what the machine is asking (the machine state's `request`, by its `id`): for a tip change, the tip now on each nozzle listed (`tips`, by nozzle ID; null for none). The running operation carries on; Stop ends it instead. Needs control |
| `POST /api/v1/machine/stop` | End the running operation before its next move (resuming if paused); the machine stays homed; any client may call it |
| `POST /api/v1/machine/estop` | The firmware's halt (`M112`); Marlin then needs a power cycle, so the Studio pauses instead; any client may call it |
| `POST /api/v1/machine/reset` | Clear a latched emergency stop |
| `GET`, `POST`, `DELETE /api/v1/lease` | See, take, or release the control lease |
| `GET /api/v1/operations/{id}` | Status of a long-running command |
| `GET /api/v1/presence` | Connected clients |
| `GET /api/v1/cameras` | Cameras and whether they are streaming, including the config's monitor cameras (`monitor`) |
| `GET /api/v1/cameras/{id}/snapshot.jpg` | One JPEG frame |
| `GET`, `PUT /api/v1/cameras/{id}/controls` | A real camera's exposure (milliseconds, or `auto`), gain and white balance (kelvin, or `auto`), with their ranges; set them now, until the core restarts (on Linux). Setting needs control |
| `GET /api/v1/cameras/{id}/stream.mjpg` | A live MJPEG stream |
| `GET`, `PUT`, `DELETE /api/v1/library/parts/{id}` | Parts (and `GET /library/parts` to list) |
| `GET`, `PUT`, `DELETE /api/v1/library/footprints/{id}` | Footprints (and `GET /library/footprints`) |
| `GET`, `PUT`, `DELETE /api/v1/library/tapes/{id}` | Tapes, which parts link to by `tape_id` (and `GET /library/tapes`); a tape in use cannot be deleted |
| `GET /api/v1/library/export`, `POST /api/v1/library/import` | The library as one JSON document |
| `POST /api/v1/library/import/openpnp` | Merge OpenPnP `packages.xml` and `parts.xml` |
| `POST /api/v1/boards/import/kicad` | Read a KiCad board (and panel, and BOM) into a board, its library entries, and a new job |
| `GET /api/v1/jobs`; `GET`, `PUT`, `DELETE /api/v1/jobs/{id}` | Saved jobs (working copies, with this machine's setup for each) |
| `GET`, `PUT /api/v1/jobs/{id}/setup` | Where this machine has a job's panel on the bed, and the board's height |
| `GET /api/v1/jobs/{id}/layout` | Where what runs for a job (a revision, else its working copy) lands on this machine |
| `POST /api/v1/jobs/layout` | Where a job's placements land on the machine, and what stops it |
| `POST /api/v1/jobs/find-location` | A place on the bed where a job fits |
| `POST /api/v1/jobs/locate` | Where a job's panel is on the bed, from one of its fiducials (`ref`, and `copy_id` for a board copy's): by default, the one the top camera is centered on now, the panel turned by `rotation` (default: the job's). Nothing is saved |
| `GET`, `POST /api/v1/jobs/{id}/revisions`; `GET /api/v1/jobs/{id}/revisions/{n}` | A job's published revisions; publish the saved job as the next one |
| `GET /api/v1/jobs/{id}/revisions/{n}/file` | A revision's job file, exactly as kept |
| `POST /api/v1/jobs/import` | Load a job file from disk: a published revision, or a job saved before revisions |
| `GET /api/v1/feeders` | Feeder slots, the feeder found in each, and where to pick |
| `POST /api/v1/feeders/scan` | Find and initialize the feeders on the bus |
| `POST /api/v1/feeders/slots/{id}/feed`, `…/identify` | Feed a feeder, or flash its light |
| `PUT`, `DELETE /api/v1/feeders/slots/{id}/pick` | Teach a slot's pick location (at the camera), or forget it |
| `POST /api/v1/feeders/slots/{id}/pocket` | Find the pick pocket from the tape's sprocket holes |
| `POST /api/v1/feeders/slots/{id}/fiducial`, `POST /api/v1/feeders/calibrate` | Measure the feeder fiducial's offset; calibrate slots from their fiducials |
| `POST /api/v1/feeders/rails/calibrate`, `POST /api/v1/feeders/slots/{id}/rail-fiducial`, `DELETE /api/v1/feeders/rails/{bank}` | Find where each feeder rail really is from its end feeders' fiducials (or one the operator centered the camera on), and how high, by touching each with a nozzle; go back to the profile's place |
| `PUT`, `DELETE /api/v1/feeders/assignments/{feeder_id}` | Which part a feeder holds (its reel count is kept while the part stays the same) |
| `PUT /api/v1/feeders/assignments/{feeder_id}/reel` | How many parts are on a feeder's reel (counted down by each feed) and the level to warn the operator at |
| `POST /api/v1/feeders/fixed`; `PUT`, `DELETE …/fixed/{id}` | Strips and trays held on the bed (T1, T2, ...): add, change, remove |
| `PUT /api/v1/feeders/fixed/{id}/teach` | Teach a strip or tray's first part, end of its first row, or last part (at the camera) |
| `POST /api/v1/feeders/fixed/{id}/refill`, `…/pocket` | Mark it refilled; find a strip's next pocket from its sprocket holes |
| `POST /api/v1/feeders/fixed/{id}/check-holes` | Check a strip was taught the way its tape feeds: its sprocket holes beside its last part, on the side its first and last parts say (fails if they are on the other side). The Studio runs it after **Last part** |
| `GET`, `POST /api/v1/recordings`; `POST …/stop` | Flight recordings: list them, start one, stop it |
| `GET /api/v1/recordings/{id}/download`; `DELETE …/{id}` | One recording as a zip file; delete it |
| `DELETE /api/v1/feeders/learned/{id}` | Forget what the bottom camera learned of a slot's, strip's, or tray's picks |
| `GET`, `PUT /api/v1/bring-up` | The bring-up checklist's progress and findings |
| `GET /api/v1/placement` | What each nozzle holds, how it sits once aligned, and the configured pick correction |
| `POST /api/v1/placement/pick`, `…/align`, `…/place` | Pick from a feeder slot; find the part on the bottom camera; place it at a location and angle |
| `POST /api/v1/placement/pick-and-place`, `…/discard`, `…/release` | All three steps at once; drop a held part in the discard bin; a part was taken off its nozzle by hand (suction off, part forgotten) |
| `POST /api/v1/placement/train` | Pick a part, try every light and way of seeing it over the bottom camera at four turns, keep the steadiest for the part (to be approved), take its approval pictures, and drop it in the discard. Needs control |
| `GET /api/v1/placement/trained` | Parts trained on this machine: how each is seen, whether approved or found in a run (used before approval), and the best ways tried |
| `GET /api/v1/placement/trained/{part_id}/images/{name}` | A trained part's picture: `overlay` (its footprint over it), `beside` (its footprint beside it), or `light-NN` (under each light tried). Takes the token as a query parameter too, for `<img>` tags |
| `POST /api/v1/placement/trained/{part_id}/approve`, `DELETE …/trained/{part_id}` | Approve what training found, so alignment uses it; or forget it. Need control |
| `POST /api/v1/run/plan` | A dry run of a job's published revision (its newest, or one given): each placement's feeder and nozzles, and what blocks any; its `parts` are the load list (each part's value, footprint, tape, references, and where it is loaded). Resuming a run plans its blocked placements again |
| `POST /api/v1/run/start`, `…/pause`, `…/step`, `…/resume`, `…/stop`; `GET /api/v1/run` | Run a job (a feeder scan, board fiducials, then batches of pick, align, and place, each feeder's fiducial found on its first pick), optionally starting in step mode, and optionally checking each two-terminal chip with the top camera once placed (`check_placements`: holding one off its pads, turned, or not there, and once every part is placed pausing with `reviewing` set and those in `to_review`; when its pads show the board has moved, pausing at once and finding the board again on resume; where it was found, how much of it is on its pads, and where the pads were found are kept with each item); its progress, and `stats`: picks tried and how many came up empty, parts the bottom camera looked for and could not find, placements, and placements checked and how many were off their pads |
| `POST /api/v1/run/items/{index}/review` | While paused (as at the review at the end of a run): leave a placed part as it is (one found off is marked fine), or, with `place_again`, place it again once the operator has taken it off |
| `GET /api/v1/run/checks/{index}` | The current run's checked placement (by plan item) as the top camera saw it: where it was sent (orange) and where it was found (magenta). Takes the token as a query parameter too, for `<img>` tags |
| `POST /api/v1/run/check-board`; `GET /api/v1/run/board-check` | Find a job's board by its fiducials and fit it, placing nothing; the last check |
| `PUT /api/v1/run/items/{index}` | Skip a placement, or take back a skip |
| `POST`, `GET /api/v1/cameras/captures`; `GET …/captures/{id}.png` | Save full-resolution frames with the machine's state; list and fetch them |

## Rules worth knowing

- **Motion needs a connected client.** Commands that move the machine or switch outputs
  must name a client connected to the event socket, in the `X-PicnicPlace-Client` header.
  That client then holds the control lease until it releases it or disconnects.
- **Long commands are operations.** Home, move, and the vision commands return an
  operation ID at once; progress and the result arrive over the event socket.
- **Units** are millimeters, degrees, and seconds everywhere.
- **Security.** The core listens on `127.0.0.1` by default. To listen on a network it
  requires a token, sent as `Authorization: Bearer <token>` (or `?token=` on camera URLs,
  since `<img>` tags can't send headers).
