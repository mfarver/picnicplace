# Code map

Where things are in the PicnicPlace repository, and how they fit together. It is written
for developers and coding agents alike. For why things are the way they are, read the
[specification](../SPEC.md); for the workflow (setup, checks, sign-off), read
[Contributing](../contributing.md).

## The big picture

```
 Studio (browser)  ──REST /api/v1 + event socket──▶  picnicd (the core)  ──▶  machine
 studio/src/                                         core/src/picnicplace/     (or the simulator)
```

- **The core** (`core/`, Python) owns the machine. Every client, the Studio included,
  talks to it over the [network API](../api/overview.md): REST for state and commands,
  one WebSocket (`/api/v1/events`) for live events. Long tasks (homing, picking, a whole
  run) are *operations*: the request returns at once, and progress comes on the socket.
- **The Studio** (`studio/`, React + TypeScript) is one client of that API. Its types
  are generated from the core's OpenAPI document, so the two cannot drift apart
  unnoticed.
- **The website** (`docs/`) is built from Markdown with Zensical and published to
  picnicplace.org on every merge to `main`.

A request goes **route → service → machine service → driver**:

1. A route in `api/` checks permissions and the control lease, and calls a service.
2. A service (`placement.py`, `feeders/service.py`, `job/run.py`) does the work, often
   as an operation started through `MachineService.start_task`.
3. `machine/service.py` (the **machine service**) is the one owner of the machine. Every
   move, output, camera frame, and vision call goes through it, and so does every safety
   rule (safe Z, homing, travel limits, E-stop, pause, resume and stop).
4. A **driver** (`machine/simulator.py`, or the Marlin driver) talks to the hardware.

New to the code? Read the next two sections, then keep the rest of this page open as a
map.

## Words used everywhere

| Word | What it means |
|---|---|
| **Operation** | A long task (homing, a pick, a whole run). Starting one returns at once with its ID; its progress and end come as `operation` events on the socket (`operations.py`). |
| **Control lease** | Only one client may move the machine at a time: the one holding the lease. A free lease is taken by the first motion request; any client may still pause (`lease.py`). |
| **Built-in profile, local profile, config** | Three layers of settings. The built-in profile is a machine model's design (`profiles/*.toml`); the local profile is what is true of one machine, written by the Studio (`machine/local.py`); the config is how the owner runs it, edited by hand (`config.py`). See [AGENTS.md](https://github.com/mfarver/picnicplace/blob/main/AGENTS.md#rules). |
| **Machine service** | `machine/service.py`: the one owner of the machine. Everything that moves it goes through here, and so does every safety rule. |
| **Driver** | What turns the machine service's moves into hardware: the simulator, or the Marlin driver (`machine/driver.py` is the interface). |
| **Slot, feeder** | A slot is a place on a feeder rail, from the profile's feeder banks; a feeder is a Photon feeder found on the bus, known by its ID, and sits in a slot. Strips and trays on the bed are **fixed feeders**. |
| **Load** | A part held on a nozzle, with what is known of it (where it came from, how it was aligned). |
| **Pick correction** | How picks are kept centered on their parts: by the top camera, the bottom camera, both, or neither. |
| **Job, revision, setup** | A job is a board or panel to assemble; its working copy is edited in Prepare. Publishing makes a **revision**, written once, and only revisions run. A job's **setup** is this machine's part: where its panel sits on the bed and how high the board is. |
| **Calibration checklist, stage** | What the machine has measured of itself, and when (`calibration.py`). From it the setup wizard gives the machine a **stage**: `limited`, `out_of_date`, or `calibrated`, and holds a limited machine to safe tasks (`wizard.py`). |
| **Flight recording** | Every frame vision looked at and everything the machine did, saved so a developer can replay today's vision on a real machine's frames (`recorder.py`). |

## One pick, through the code

Following the Studio's **Pick** button shows most of the core's shape. Every task works
the same way: a run in `job/run.py` calls the same placement code for each part.

1. **The Studio** (`components/PlacementPanel.tsx`) posts to `/api/v1/placement/pick`
   with the slot and nozzle, through the typed client (`api/client.ts`). It gets an
   operation back and keeps its ID.
2. **The route** (`api/placement.py`) depends on `require_control`
   (`api/context.py`): the client must hold the control lease, or a free one is taken
   for it. The route calls `PlacementService.start_pick`.
3. **The placement service** (`placement.py`) checks what it can before anything moves
   (the nozzle is empty, the slot has a part) so a mistake is a clear error, not a
   failed operation. Then it calls `MachineService.start_task("pick", ...)`.
4. **`start_task`** (`machine/service.py`) refuses if the machine cannot move or is not
   homed, asks the setup wizard's limits whether a pick is allowed at this stage, marks
   the machine busy, and starts the work as an operation (`operations.py`). The HTTP
   request answers `202 Accepted` here.
5. **The pick itself** runs in the background (`PlacementService.pick`): the feeder
   service feeds the part and works out where its pocket is (`feeders/service.py`,
   maybe looking at the sprocket holes with the top camera), and `_take` moves the
   nozzle over it, lowers it, turns the suction on, raises it, and checks the vacuum
   sensor to see the part was taken.
6. **Each move** goes through `MachineService.move_axes`. `_plan_move` splits a move
   that travels in X or Y into steps: up to safe Z, across, then down. `_run_steps`
   checks each step before sending it: it waits there while the machine is paused, ends
   the task if Stop was asked for, and refuses to travel unless the nozzles are up (the
   safe-Z sensor decides, on a machine that has one). Then the step goes to the flight
   recorder and to the driver.
7. **The driver** moves the machine. The simulator moves its model and knows where the
   part really is (`simparts.py`); the Marlin driver (`machine/marlin/driver.py`) sends
   `G0` and waits for `M400`.
8. **The operation ends.** `operations.py` stores it as succeeded, or failed with the
   error's message, written for the user, and publishes it on the event bus
   (`events.py`). The socket (`api/websocket.py`) sends it to every client, and the
   machine's state (busy, now holding a part) goes with it.
9. **The Studio** keeps every operation it hears of (`api/core.ts`). The Placement panel
   finds its own by ID, shows the message or error, and enables its buttons again.

To see it happen, run `make core`, open the Studio's Machine workspace, and pick a part
with the simulator, or read `test_placement.py`, which does the same through the API.

## Repository layout

| Path | What is there |
|---|---|
| `core/` | The core: the `picnicd` command, its API, the machine service, drivers, vision, feeders, jobs |
| `core/src/picnicplace/` | The Python package (the map below) |
| `core/src/picnicplace/profiles/` | Built-in machine profiles (`lumenpnp-v4.1.toml`): the design facts of a machine model |
| `core/tests/` | The core's tests, all against the simulator (no hardware needed) |
| `core/picnicd.example.toml` | Every config setting, explained: copy it to make a config |
| `studio/src/` | The Studio (the map below) |
| `docs/` | The website: specification, guides, hardware notes, API overview, and the generated `api/openapi.json` |
| `hardware/` | Boards for owners to order (the datum and the test board), each a KiCad file generated by a script next to it; licensed CERN-OHL-S-2.0 |
| `zensical.toml` | The website's settings and navigation |
| `Makefile` | `make setup`, `make core`, `make dev`, `make api`, `make check`, `make docs` |
| `.github/workflows/` | CI: what `make check` runs, plus the API-is-up-to-date check and the website build |

## The core: `core/src/picnicplace/`

### Running and wiring

| Module | What it does |
|---|---|
| `cli.py` | The `picnicd` command: flags that override the config, `--print-openapi`, `--replay` |
| `config.py` | The config file (TOML), validated with Pydantic. Owner choices live here (discard and park locations, pick correction, cameras and monitor cameras, recording). |
| `api/app.py` | Builds the application: creates every service and wires them together. **Start here** to see what depends on what. |
| `api/context.py` | The shared objects (`AppContext`) and the FastAPI dependencies: authentication, permissions, the control lease |
| `api/*.py` | One router per area: `routes.py` (machine), `cameras.py`, `library.py`, `boards.py`, `jobs.py`, `feeders.py`, `placement.py`, `run.py`, `recordings.py`, `websocket.py` |
| `schemas.py` | Models that appear on the wire: machine state, operations, events |
| `events.py` | The in-process event bus that feeds the event socket |
| `operations.py` | Long tasks as operations, with their status published as events |
| `presence.py`, `lease.py`, `auth.py` | Who is connected, who has control, and what each client may do |
| `storage.py` | Small helpers for the data directory's files, such as writing a file whole or not at all |

### The machine

| Module | What it does |
|---|---|
| `machine/profile.py` | Machine profiles: axes, nozzles, cameras, feeder banks, locations, keep-outs. Built-in profiles hold a model's **design facts only**, and can `extends` another. |
| `machine/local.py` | The local profile: what is true of one machine (discard and park locations, measured keep-outs), laid over the built-in profile. Written by the Studio. |
| `machine/service.py` | The machine service: the one owner of the machine, and where the safety rules live. Also camera frames and vision calls for tasks, and asking the operator mid-task (`ask`, `change_tips`). |
| `machine/bottom_camera.py` | Calibrating the bottom camera with a nozzle tip: its place, focus height, scale, and turn, and how big the fitted tip looked; naming the tip on each nozzle by the size of its end |
| `machine/sniff.py` | Finding a height by touch: a nozzle steps down with suction on until the vacuum jumps from its open reading (the calibration disc, a board's fiducial, a part in its pocket) |
| `machine/tips.py` | Which nozzle tip is fitted on each nozzle, kept in the data directory |
| `machine/nozzle_offsets.py` | Calibrating the nozzle offsets: sniffing for the disc's top on the datum board, then turning the disc with each nozzle; each nozzle's vacuum levels measured on the way |
| `machine/driver.py` | The interface every motion driver implements |
| `machine/simulator.py` | The simulated machine. Its errors are realistic (homing error, feeder steps, slot and board offsets, cameras and nozzles off the profile, tips longer than the profile's, the datum board's height), so calibration and vision have something to find. |
| `machine/marlin/` | The Marlin driver (`driver.py`: one G-code command at a time, each waiting for its `ok`), the serial port (`transport.py`), how a profile's `[marlin]` table tells it what to send (`settings.py`), and a fake LumenPnP firmware that answers the way a real one does (`fake.py`) |
| `machine/factory.py` | Picks the driver the config asks for (simulator, Marlin, or Marlin against a fake firmware) |
| `machine/errors.py` | Machine errors; their messages are written to be shown to users |
| `probe.py` | `picnicplace-probe`: records what a new machine is, without moving it |

### Cameras and vision

| Module | What it does |
|---|---|
| `camera/` | Camera sources (`device.py` for real cameras, `synthetic.py` for the simulator's rendered views), a hub per camera that shares frames among viewers, and `v4l2.py`, which asks a Linux camera what formats, sizes, and frame rates it can send (for the probe) |
| `vision/fiducial.py` | Finds round marks: homing and board fiducials, feeder fiducials, sprocket holes |
| `vision/part.py` | The metal mask (bright, not yellowish plastic, no specks), and one box around a part's metal, for parts without a footprint |
| `vision/pads.py` | Fits a footprint's pads to the metal in a bottom camera frame, for parts whose pads are off their body's center or whose body is as bright as their leads (bottom vision's finder whenever a footprint has two pads or more) |
| `vision/focus.py` | How sharp part of a frame is, for finding a camera's focus |
| `capture.py` | Saves single frames on request, for tuning vision on real images |
| `calibration.py` | The calibration checklist: what the machine has measured of itself and when, using it in place of the profile's values, what a crash makes out of date, and what stops a run |
| `machine_setup.py` | The setup wizard's choices (machine, serial port, cameras), laid under the config when `picnicd` starts; listing serial ports and cameras; probing a port without moving anything |
| `wizard_run.py` | Running a stop of the setup wizard: its steps one after another, as one operation, with progress for the Studio |
| `wizard.py` | The setup wizard: each page's state (from the running machine, the checklist, the bring-up checklist's steps, and the pages the owner confirms), the machine's stage, and the limits `MachineService.start_task` holds it to until calibrated |
| `recorder.py` | The flight recorder: every frame vision looked at and everything the machine did, and `replay_vision` to run today's vision on recorded frames |

### Parts, feeders, boards, and jobs

| Module | What it does |
|---|---|
| `library/` | The parts library (SQLite): footprints (with pin 1 marked), tapes that parts link to, and parts (`models.py`, `store.py`, which converts older libraries); EIA-481 tape geometry (`tape.py`); OpenPnP library import (`openpnp.py`) |
| `feeders/slots.py` | Feeder slots, from the profile's feeder banks |
| `feeders/photon.py`, `feeders/sim.py` | The Photon feeder protocol (RS-485), and simulated Photon feeders |
| `feeders/setup.py` | The owner's feeder setup (`feeders.json`): parts in feeders, rail and slot calibrations, strips and trays, what the bottom camera learned |
| `feeders/service.py` | The feeder service: scanning, feeding, where to pick, fiducial calibration, pocket finding, strips and trays, pick learning |
| `feeders/pocket.py` | Where the pocket is, from the sprocket holes seen |
| `feeders/rails.py` | Fitting a feeder rail to the feeder fiducials found on it |
| `placement.py` | Pick, align, and place one part; pick correction (top or bottom camera) |
| `simparts.py` | The simulator's parts: which nozzle holds what, where placed parts landed (and how far they fell), the calibration disc, and crashes: a tip pressed into a part, or a part into the board. Part tops come from the feeders' tapes, strips, and trays, and board tops from the staging plates, each off by the config's simulator errors |
| `board/` | Boards and panels, and reading them from KiCad (`kicad.py`, with a small S-expression reader) and KiKit panels |
| `job/models.py`, `job/store.py` | Jobs (a board or panel on the machine) and their files, with their published revisions and this machine's setup for each |
| `job/revision.py` | Job revisions: the design and a copy of the library entries it uses, the job file, and its hash |
| `job/layout.py` | Where a job's placements land on the machine, and what the machine cannot reach |
| `job/run.py` | Running a job's published revision: the planner (also the dry run), board alignment, batches, pause, step, resume, stop, refills |

### The data directory

The core keeps the owner's data in one folder (`data.dir` in the config; by default
`~/.local/share/picnicplace`):

| Path | What it holds | Code |
|---|---|---|
| `library.sqlite3` | The parts library | `library/store.py` |
| `feeders.json` | The feeder setup | `feeders/setup.py` |
| `jobs/` | One JSON file per job (its working copy), and a folder per job of its published revisions, each written once | `job/store.py` |
| `setups/` | One JSON file per job: where this machine has its panel on the bed, and the board's height | `job/store.py` |
| `captures/` | Frames saved with the Capture panel | `capture.py` |
| `recordings/` | Flight recordings | `recorder.py` |
| `bring-up.json` | Progress through the bring-up checklist | `bringup.py` |
| `wizard.json` | The setup wizard's pages the owner confirmed or skipped | `wizard.py` |
| `machine-setup.json` | The setup wizard's choices: the machine, its serial port and cameras, and the last probe | `machine_setup.py` |
| `calibration.json` | What the machine has measured of itself (homing, the homing fiducial, the top camera's scale, the bottom camera, the nozzle offsets, each nozzle's Z offset and vacuum levels for each tip), when, and what is out of date | `calibration.py` |
| `nozzle-tips.json` | Which tip is fitted on each nozzle | `machine/tips.py` |

## The tests: `core/tests/`

Nearly every test runs against the simulator, usually through the real API with FastAPI's test
client. Useful pieces:

- `conftest.py`: `fast_config()` (a fast simulator and a temporary data directory), and
  `connect()`, a session holding the control lease, with `s.home()` and `s.wait(op)` to
  wait for an operation to end.
- `test_placement.py`, `test_run.py`, `test_fixed_feeders.py`: `set_up(...)` helpers
  that put parts in the library and on feeders, and (`test_run.py`) a demo board job.
- The simulator knows where every part really is, so tests check **where a part
  landed**, not what the core believes (`placed(client)`, `check_landed(...)`).
- `tests/data/`: sample boards (KiCad, KiKit) and other inputs; `tests/data/vision/`:
  real camera frames that `test_bottom_vision_frames.py` runs the vision functions on;
  `tests/profiles/`: a test machine profile.

| Area | Tests |
|---|---|
| API, events, presence, config, I/O | `test_api.py`, `test_events.py`, `test_presence.py`, `test_config.py`, `test_io.py` |
| Machine profiles, the Marlin driver, the probe | `test_profile.py`, `test_marlin.py`, `test_probe.py` |
| Cameras and vision | `test_cameras.py`, `test_vision.py` |
| Library, boards, jobs | `test_library.py`, `test_tapes.py`, `test_board.py`, `test_job.py` |
| Feeders, placement, runs | `test_feeders.py`, `test_fixed_feeders.py`, `test_placement.py`, `test_pick_correction.py`, `test_run.py` |
| Flight recorder | `test_recorder.py` |

## The Studio: `studio/src/`

| Path | What is there |
|---|---|
| `App.tsx` | The shell: the top bar's workspaces, the status bar, Pause and Resume |
| `workspaces/` | One screen per tab: `PrepareWorkspace` (boards and jobs), `LibraryWorkspace` (Parts), `FeedersWorkspace`, `RunWorkspace`, `MachineWorkspace` |
| `components/` | Panels and controls used by the workspaces; `machine/` holds the machine view (the overhead map and head inset), `wizard/` the setup wizard (a panel over the map) and the calibration banner, `job/` and `library/` their own pieces |
| `api/schema.ts` | **Generated** TypeScript types for the whole API (`make api`); never edit by hand |
| `api/types.ts` | Short names for the generated types |
| `tape.ts`, `runSteps.ts` | Pure logic with unit tests: tape geometry, fit check, pin-1 quadrant, and table-cell parsing; a run's step list |
| `mjpeg.ts` | Reads a camera's MJPEG stream with `fetch`, so leaving a view closes its connection (an `<img>` can hold it open and use up the browser's six connections to the core) |
| `api/client.ts` | The typed REST client (`api.GET("/api/v1/...")`) |
| `api/core.ts`, `api/CoreProvider.tsx` | The event socket and the live state it feeds: machine state, operations, presence, the lease |
| `api/use*.ts` | Hooks that fetch and follow one kind of state, such as `useFeeders` and `useCameras` |
| `styles/` | `tokens.css` (colors, spacing; light and dark) and `app.css` |

## Common changes

- **A new endpoint or a changed model:** change the core, then run `make api` and commit
  `docs/api/openapi.json`, `studio/src/api/schema.ts`, and `fingerprint.ts`. Add the
  endpoint to the [API overview](../api/overview.md).
- **A new config setting:** add it to `config.py` with a description, and explain it in
  `core/picnicd.example.toml`.
- **A fact about a machine's design:** the profile (`profiles/*.toml` and
  `machine/profile.py`). A choice the owner makes belongs in the config instead.
- **A new vision function used by a task:** call it through
  `machine.recorder.vision(...)` and register it in `recorder.VISION_FUNCTIONS`, so
  flight recordings keep its frames and can replay it.
- **Anything a user does:** update the [specification](../SPEC.md) and the guide it
  touches ([bring-up](../guide/bring-up.md), [simulator](../guide/try-the-simulator.md)),
  and the milestone tables in the spec and on the [home page](../index.md).
- **Before a pull request:** `make check` runs every linter, type checker, and test
  suite, as CI does.
