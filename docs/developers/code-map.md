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
   rule (safe Z, homing, travel limits, E-stop).
4. A **driver** (`machine/simulator.py`, or the Marlin driver) talks to the hardware.

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
| `zensical.toml` | The website's settings and navigation |
| `Makefile` | `make setup`, `make core`, `make dev`, `make api`, `make check`, `make docs` |
| `.github/workflows/` | CI: what `make check` runs, plus the API-is-up-to-date check and the website build |

## The core: `core/src/picnicplace/`

### Running and wiring

| Module | What it does |
|---|---|
| `cli.py` | The `picnicd` command: flags that override the config, `--print-openapi`, `--replay` |
| `config.py` | The config file (TOML), validated with Pydantic. Owner choices live here (discard and park locations, pick correction, cameras, recording). |
| `api/app.py` | Builds the application: creates every service and wires them together. **Start here** to see what depends on what. |
| `api/context.py` | The shared objects (`AppContext`) and the FastAPI dependencies: authentication, permissions, the control lease |
| `api/*.py` | One router per area: `routes.py` (machine), `cameras.py`, `library.py`, `boards.py`, `jobs.py`, `feeders.py`, `placement.py`, `run.py`, `recordings.py`, `websocket.py` |
| `schemas.py` | Models that appear on the wire: machine state, operations, events |
| `events.py` | The in-process event bus that feeds the event socket |
| `operations.py` | Long tasks as operations, with their status published as events |
| `presence.py`, `lease.py`, `auth.py` | Who is connected, who has control, and what each client may do |

### The machine

| Module | What it does |
|---|---|
| `machine/profile.py` | Machine profiles: axes, nozzles, cameras, feeder banks, locations, keep-outs. Built-in profiles hold a model's **design facts only**, and can `extends` another. |
| `machine/local.py` | The local profile: what is true of one machine (discard and park locations, measured keep-outs), laid over the built-in profile. Written by the Studio. |
| `machine/service.py` | The machine service: the one owner of the machine, and where the safety rules live. Also camera frames and vision calls for tasks. |
| `machine/bottom_camera.py` | Calibrating the bottom camera with a nozzle tip: its place, focus height, scale, and turn |
| `machine/nozzle_offsets.py` | Calibrating the nozzle offsets: sniffing for the datum board's height, then turning a disc on it with each nozzle |
| `machine/driver.py` | The interface every motion driver implements |
| `machine/simulator.py` | The simulated machine. Its errors are realistic (homing error, feeder steps, slot and board offsets, cameras and nozzles off the profile, tips longer than the profile's, the datum board's height), so calibration and vision have something to find. |
| `machine/factory.py` | Picks the driver the config asks for (simulator, Marlin, or Marlin against a fake firmware) |
| `machine/errors.py` | Machine errors; their messages are written to be shown to users |
| `probe.py` | `picnicplace-probe`: records what a new machine is, without moving it |

### Cameras and vision

| Module | What it does |
|---|---|
| `camera/` | Camera sources (`device.py` for real cameras, `synthetic.py` for the simulator's rendered views), and a hub per camera that shares frames among viewers |
| `vision/fiducial.py` | Finds round marks: homing and board fiducials, feeder fiducials, sprocket holes |
| `vision/part.py` | Finds a part on a nozzle, in a bottom camera frame |
| `vision/focus.py` | How sharp part of a frame is, for finding a camera's focus |
| `capture.py` | Saves single frames on request, for tuning vision on real images |
| `calibration.py` | The calibration checklist: what the machine has measured of itself and when, using it in place of the profile's values, what a crash makes out of date, and what stops a run |
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
| `simparts.py` | The simulator's parts: which nozzle holds what, where placed parts landed (and how far they fell), the calibration disc, and crashes: a tip pressed into a part, or a part into the board |
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
| `calibration.json` | What the machine has measured of itself (homing, the homing fiducial, the top camera's scale, the bottom camera, the nozzle offsets), when, and what is out of date | `calibration.py` |

## The tests: `core/tests/`

Every test runs against the simulator, usually through the real API with FastAPI's test
client. Useful pieces:

- `conftest.py`: `fast_config()` (a fast simulator and a temporary data directory), and
  `connect()`, a session holding the control lease, with `s.home()` and `s.wait(op)` to
  wait for an operation to end.
- `test_placement.py`, `test_run.py`, `test_fixed_feeders.py`: `set_up(...)` helpers
  that put parts in the library and on feeders, and (`test_run.py`) a demo board job.
- The simulator knows where every part really is, so tests check **where a part
  landed**, not what the core believes (`placed(client)`, `check_landed(...)`).
- `tests/data/`: sample boards (KiCad, KiKit) and other inputs; `tests/profiles/`: a
  test machine profile.

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
| `App.tsx` | The shell: the top bar's workspaces, the status bar, E-stop |
| `workspaces/` | One screen per tab: `PrepareWorkspace` (boards and jobs), `LibraryWorkspace` (Parts), `FeedersWorkspace`, `RunWorkspace`, `MachineWorkspace` |
| `components/` | Panels and controls used by the workspaces; `machine/` holds the machine view (the overhead map and head inset), `job/` and `library/` their own pieces |
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
