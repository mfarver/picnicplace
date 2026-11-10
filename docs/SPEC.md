# PicnicPlace — Specification (Draft 0.1)

> Status: **Draft**. Sections marked **[OPEN]** need a decision before work in that area starts.
> Last updated: 2026-11-09

## 1. Summary

PicnicPlace is an open source pick-and-place (PnP) machine controller in the spirit of
OpenPnP. It differs in one structural way: it is **network first**. A headless **core**
process owns the machine (motion, vision, feeders, job execution), and every user interface
is a **client** that talks to the core over a documented network API. The flagship
interface is a modern GUI with a visual style inspired by OrcaSlicer.

The first-class hardware target is the **Opulo LumenPnP**.

## 2. Goals

1. **Separation of control and interface.** The core runs headless on the machine's host
   computer (a PC, mini PC, or SBC). GUIs, CLIs, scripts, and automation connect over the
   network. Closing or crashing a GUI never stops or corrupts a running job.
2. **A documented, versioned API** that third parties can build against (custom UIs, MES
   integration, Home Assistant/OctoPrint-style dashboards, scripted calibration).
3. **A modern, attractive GUI** with sensible defaults, guided calibration, and clear
   visual feedback, while still exposing advanced settings.
4. **Strong vision** built on OpenCV: fiducials, top-camera part and feeder vision, and
   bottom-camera part alignment.
5. **Linux first.** The 1.0 core targets Linux. The core avoids Linux-only assumptions
   outside of driver and camera backends, so macOS and Windows can be added later (see §9).
6. **Approachable for contributors.** A popular language, conventional tooling, a
   simulator so most work needs no hardware, and good tests.
7. **Reliable operation that lowers the operator's load.** A primary reason for the
   rewrite. The machine handles routine trouble itself: it finds pockets and fiducials
   by vision, learns where feeders really put their parts, retries, and counts what is
   left. It asks the operator only for what needs a person (loading parts, clearing a
   jam, a decision it cannot make), in a dialog that says plainly what is wrong and what
   to do, and warns before a run runs short rather than during it. Calibration is as
   hands-off as running a job. The operator's screen shows only
   what running the machine needs (§8.2).

## 3. Non-goals (for 1.0)

- Real-time motion planning on the host. Step generation, acceleration, and kinematics
  stay in machine firmware (Marlin, Klipper, grblHAL, etc.). The core sends motion
  commands; it does not generate step pulses.
- Configuration compatibility with OpenPnP's `machine.xml`. An **importer** for OpenPnP
  configs and part/package libraries is a desirable later feature, not a constraint on
  the design.
- Multi-machine fleet management. The API should not preclude it, but 1.0 targets one core
  per machine.
- PCB design or CAM features beyond importing placement data.

## 4. Language and technology choice

### 4.1 Requirements

| Requirement | Weight |
|---|---|
| Cross platform (Linux for 1.0; macOS and Windows later) | High |
| First-class OpenCV support, hardware acceleration where available | Must |
| Large contributor pool / easy onboarding | High |
| Adequate performance for vision and serial I/O at PnP rates | Must |
| Good networking/API ecosystem | High |

### 4.2 Candidates

| Language | OpenCV support | Acceleration | Contributor pool | Notes |
|---|---|---|---|---|
| **Python** | Official bindings (`cv2`), the most-used OpenCV interface | OpenCL via T-API (`cv2.UMat`) in standard wheels; CUDA needs a custom build | Very large; common among hardware hobbyists | GIL limits CPU parallelism, but OpenCV and NumPy release it. Packaging a desktop app is weaker, which matters less for a headless core. |
| **C++** | Native; OpenCV is written in it | Full: CUDA, OpenCL, Metal via backends, vendor libraries | Large but onboarding is slow | Best raw performance. Highest friction for contributors (build systems, memory safety, slower iteration). |
| C# / .NET 8+ | OpenCvSharp, Emgu CV (third-party wrappers) | Partial, depends on wrapper build | Large | Good cross-platform runtime and async story. Wrappers lag upstream and are less widely used. |
| Rust | `opencv` crate (generated bindings) | Partial | Smaller, growing | Excellent safety and performance; bindings are awkward and builds are painful on Windows/macOS. |
| Java | Official bindings | Limited | Large | What OpenPnP uses. No clear advantage over staying with OpenPnP. |
| Go | GoCV | Partial (CUDA build) | Medium | Nice for network services; vision ecosystem is thin. |

### 4.3 Decision: Python for the core, TypeScript for the GUI

**Core (machine control + vision): Python 3.12+.**

- PnP vision workloads are light: a handful of 1–5 MP frames per placement, processed
  with classic algorithms (thresholding, contours, template matching, minimum-area
  rectangles, circle detection). OpenCV does the heavy lifting in native code, so Python's
  overhead is not the bottleneck; camera latency and machine motion are.
- The real-time-sensitive work (step generation) lives in firmware. The host's job is
  orchestration: send G-code, wait for acknowledgments, read sensors, grab frames. Python's
  `asyncio` fits this well.
- Python is the most common language for OpenCV examples, tutorials, and research, and the
  most likely language for a hobbyist contributor to already know. It is also the natural
  language for user scripting, which OpenPnP supports through JavaScript/Jython.
- Acceleration: the standard `opencv-python` wheels support OpenCL through the T-API, which
  covers integrated and discrete GPUs on all three OSes. Users with NVIDIA hardware can
  install a CUDA-enabled OpenCV build. The vision layer will be written so either path works
  without code changes (see §7.5).
- Escape hatch: if a specific pipeline stage proves too slow, it can be moved to a C++
  extension (via `nanobind`/`pybind11`) without changing the architecture.

**GUI: TypeScript web app** (see §8). Because of the network split, the GUI language is
independent of the core language. A web front end gives the widest pool of UI contributors,
the richest set of modern design tools, and lets the same GUI run in a browser on a tablet
or laptop or as a packaged desktop app.

**Considered and rejected for now:** C++ as the core language. It would give the best
performance and acceleration, but it raises the barrier for contributors more than the
performance gain is worth for this workload. Revisit only if profiling on real hardware
shows Python cannot hold target throughput.

### 4.4 Core stack

| Concern | Choice | Notes |
|---|---|---|
| Runtime | CPython 3.12+ | Evaluate free-threaded builds (3.13+) later. |
| Concurrency | `asyncio` | Vision runs in a thread pool; OpenCV releases the GIL. |
| API server | FastAPI (on Starlette/Uvicorn) | OpenAPI schema generated from code. |
| Schemas & config validation | Pydantic v2 | One source of truth for API models and config files. |
| Serial | `pyserial` + async wrapper | |
| Vision | `opencv-python` (or `opencv-contrib-python`), NumPy | |
| Camera capture | OpenCV `VideoCapture` with per-OS backend (V4L2, AVFoundation, Media Foundation) | Abstracted behind a `Camera` interface so other backends (e.g. GenICam) can be added. |
| Persistence | SQLite (job history, logs, part library) + human-editable TOML for machine config | TOML: unambiguous types, comments, `tomllib` in the standard library. |
| Tooling | `uv`, `ruff`, `pyright`/`mypy` (strict), `pytest` | Type hints required in the core. |
| Distribution | `pip`/`uv` install; Docker image for Linux; later PyInstaller/Briefcase bundles | |

## 5. Architecture

```
 ┌──────────────────────────┐   ┌─────────────┐   ┌───────────────┐
 │  PicnicPlace Studio (GUI)│   │  CLI (picnic)│   │ Scripts / 3rd │
 │  browser or desktop shell│   │              │   │ party clients │
 └────────────┬─────────────┘   └──────┬──────┘   └───────┬───────┘
              │   HTTPS REST + WebSocket events + video   │
              └──────────────────┬────────────────────────┘
                                 │
 ┌───────────────────────────────▼───────────────────────────────┐
 │                       PicnicPlace Core (picnicd)               │
 │  ┌──────────┐ ┌───────────┐ ┌───────────┐ ┌────────────────┐   │
 │  │ API layer│ │ Job engine│ │ Vision    │ │ Config & data  │   │
 │  │ auth,    │ │ planner,  │ │ pipelines,│ │ machine, parts,│   │
 │  │ sessions │ │ executor  │ │ cameras   │ │ packages, jobs │   │
 │  └────┬─────┘ └─────┬─────┘ └─────┬─────┘ └────────────────┘   │
 │       └────────┬────┴─────────────┘                            │
 │        ┌───────▼────────┐                                      │
 │        │ Machine model  │  heads, nozzles, axes, cameras,      │
 │        │                │  feeders, actuators, sensors         │
 │        └───────┬────────┘                                      │
 │        ┌───────▼────────┐                                      │
 │        │ Drivers        │  Marlin (LumenPnP), Klipper, grblHAL,│
 │        │                │  Simulator; Photon feeder bus        │
 │        └───────┬────────┘                                      │
 └────────────────┼──────────────────────────────────────────────┘
                  │ USB serial / USB video
          ┌───────▼────────┐
          │  Machine       │
          └────────────────┘
```

### 5.1 Process model

- **`picnicd`** is a single long-running process per machine. It can run as a systemd
  service, a launchd agent, a Windows service, or in a terminal for development.
- It serves the GUI's static files, so a user can browse to `http://<host>:<port>` with no
  install on the client.
- All machine access goes through one owner task per device (serial port, camera) to avoid
  interleaved commands.

### 5.2 Safety principles

- **The core owns safety.** Soft limits, collision-safe Z, vacuum checks, and motion
  interlocks are enforced in the core, never only in a client.
- **Motion requires a connected client.** The core enables motion only while at least one
  client holds a live event-socket connection. See §5.3.
- **Emergency stop** is a dedicated, highest-priority API call and WebSocket message that
  bypasses the command queue and sends the firmware's immediate stop (e.g. `M112` or
  realtime reset). A physical E-stop, or the power switch on machines without one (such
  as the LumenPnP), remains the primary safety device; the software stop is a
  convenience. Firmware without an emergency parser acts on it only after the current
  move.
- **Safe-Z interlock.** A machine profile may declare a physical safe-Z input (a switch
  that reads true when every nozzle is at or above safe-travel height). When present it is
  authoritative:
  - X/Y motion is refused unless the input reads safe, whatever the computed Z position.
  - On power-up or reconnect, if it reads not safe, Z must be raised or homed before any
    X/Y motion, including X/Y homing.
  - If the computed Z position says safe but the input says not safe, Z has lost steps.
    The core stops motion, reports a fault, and requires re-homing.
  - Machines without the input use a computed safe-Z state, and clients label it as
    computed rather than sensed.
  - After homing, Z must read the profile's `home_z` (where the nozzles are level). If
    the firmware calls level something else, every height would be off and the lift to
    safe Z could drive a nozzle down, so homing fails and the machine stays unhomed.
  - The core can only check the input before sending a move. A firmware-level interlock
    (firmware refuses or halts X/Y motion while the input is unsafe) is the stronger
    guarantee and is recommended where firmware supports it.
- **Moves travel at safe Z.** A move that changes X or Y first raises every nozzle to
  travel height (if any is below safe Z), then moves X, Y, and rotation, then lowers Z to
  its target. Jogs never move Z on their own: an X or Y jog with a nozzle below safe Z is
  refused, so the operator decides when Z moves.
- **Control lease.** Many clients may observe; only one holds the **control lease** at a
  time and can move the machine or switch outputs. Leases can be requested, handed over,
  or forcibly taken with confirmation.
  - A client that commands motion while no one holds the lease takes it implicitly, so a
    single operator never has to think about it.
  - Taking the lease from another client needs an explicit `force`, which clients confirm
    with the user first.
  - Motion requests name the sending client (the ID from the event socket's `hello`,
    in the `X-PicnicPlace-Client` header). The client must be connected; one-shot
    requests from nowhere cannot move the machine.
  - The emergency stop needs no lease: any client can stop the machine.

### 5.3 Client presence rules

- **Presence** means at least one authenticated client has an open WebSocket on
  `/api/v1/events` and is answering heartbeats. One-shot REST calls do not count.
  Observer clients count; the point is that a person can see the machine.
- **Manual motion** (jog, move, actuators) is allowed only from the lease holder while it
  is connected. If the lease holder disconnects, manual motion stops immediately and the
  lease is released.
- **Jobs survive disconnects but pause.** If no clients are present, a running job
  pauses. The job is not ended and its progress is kept.
- **Grace period.** The pause triggers after the last client has been gone for a short,
  configurable time (default 5 s), so a browser reload or a Wi-Fi blip does not interrupt
  a job.
- **Safe pause point.** The job pauses at the next safe point: the current move finishes,
  the head goes to safe Z, and vacuum stays on so a part already picked is held on the
  nozzle. A part on the nozzle is not placed before pausing; the job continues with it
  on resume. (A vacuum check runs on resume in case the part was lost while paused.)
- **Resume is manual.** A job paused for lost presence resumes only when a client
  reconnects and an operator chooses Resume. It never resumes automatically.
- The CLI's `run` command stays connected while the job runs (it follows progress), so
  it counts as presence. Detaching from a job leaves it to pause unless another client is
  connected.

## 6. Network API

### 6.1 Transport

| Channel | Purpose | Format |
|---|---|---|
| REST over HTTP(S) | Resources and commands (config, parts, jobs, motion requests) | JSON, described by OpenAPI 3.1 |
| WebSocket `/api/v1/events` | Server-to-client state, progress, logs, alerts; low-latency jog/E-stop from client | JSON messages with a `type` field |
| Video | Live camera feeds | MJPEG over HTTP (always available) and WebRTC (preferred for Studio). See §6.5. |

Alternatives considered: gRPC (strong typing, streaming) was rejected as the primary API
because browsers cannot call it directly. MQTT may be offered later as an optional bridge
for factory/home automation.

### 6.2 Principles

- **Versioned** under `/api/v1/…`. Breaking changes require a new version.
- **Schema first.** The OpenAPI document is generated from Pydantic models and published.
  The GUI's TypeScript client is generated from it, so the GUI cannot drift from the API.
- **Long-running commands are operations.** Commands such as `home`, `calibrate`, or
  `run job` return an operation ID immediately; progress and results stream over the event
  socket and are also available at `/api/v1/operations/{id}`.
- **Everything the GUI can do, the API can do.** The GUI uses no private endpoints.
- **Units:** millimeters, degrees, seconds, in all API payloads and config files.

### 6.3 Security

- Default bind is `127.0.0.1`. Binding to the network is an explicit setting.
- When network-bound: token authentication (API tokens for scripts; login for the GUI),
  and TLS with a self-signed certificate generated on first run, or a user-supplied one.
- **1.0: single shared secret.** One token/password grants full access. This fits the
  common setup of one operator with a GUI running at the machine.
- **Built for more later.** Every request is resolved internally to a `Principal`
  (identity, client kind, and permission set), and every endpoint declares the
  permission it needs (`view`, `motion`, `job`, `config`). In 1.0 the one principal has
  all permissions, so adding the features below does not change the API.

#### Planned next: local-only motion

Expected soon after 1.0: remote clients can watch, but anything that causes motion must
come from a client flagged as **local**, meaning physically at the machine.

- **Local clients:**
  - Connections over loopback (a GUI running on the machine's host computer) are local
    automatically.
  - Other devices, such as an ESP32 touchscreen mounted on the machine, become local by
    **pairing at the machine**: the device shows a short code that is confirmed on an
    already-local client (or the reverse). Pairing issues that device its own token
    flagged local, which can be revoked.
  - IP address or subnet alone is not used to decide "local," since it is easy to spoof
    on a LAN.
- **Remote clients** get `view` (and optionally `job` actions that do not move the
  machine, such as editing a job), but not `motion`.
- The control lease (§5.2) can only be held by a local client.
- Presence (§5.3) may later be tightened to require a local client, not just any client.

#### Later: user roles

`observer` (view only), `operator` (run jobs, jog), and `admin` (change config, manage
users), layered on the same permission model.

### 6.4 Sketch of resources

```
GET    /api/v1/machine                 machine description and live state
POST   /api/v1/machine/home
POST   /api/v1/machine/estop
POST   /api/v1/machine/jog             (also available over the WebSocket)
POST   /api/v1/machine/move
GET    /api/v1/cameras                 list; /cameras/{id}/stream for video
POST   /api/v1/cameras/{id}/snapshot
POST   /api/v1/vision/run              run a pipeline against a camera, return results + debug images
GET/PUT /api/v1/config/...             machine configuration
CRUD   /api/v1/parts, /packages, /feeders
CRUD   /api/v1/jobs                    import boards, edit placements
POST   /api/v1/jobs/{id}/run | pause | resume | stop | step
GET    /api/v1/operations/{id}
POST   /api/v1/lease                   request/release control lease
```

### 6.5 Video streaming

Live video is for people only. Vision always runs inside the core on raw, uncompressed
frames, so stream compression never affects placement accuracy.

The core offers two transports from one capture pipeline per camera:

| | MJPEG over HTTP | WebRTC |
|---|---|---|
| Role | Universal fallback; embedded and simple clients | Default for Studio |
| Client support | Anything that can decode JPEG: browsers, `<img>` tags, scripts, ESP32-S3/P4 displays | Browsers, native WebRTC libraries |
| Latency on LAN | Low (one frame plus encode) | Low (about 100–200 ms with jitter buffer) |
| Bandwidth | High (roughly 5–20 Mbit/s per 1080p stream) | Low and adaptive (roughly 1–4 Mbit/s) |
| Wi-Fi / remote | Struggles | Good; adapts to the link |
| Host cost | JPEG encode is cheap; often native from UVC cameras | H.264/VP8 encode. The Raspberry Pi 5 has no hardware video encoder, so this is software encode there. |
| Complexity | Very low | Signaling, ICE, codec negotiation (`aiortc`) |

Requirements:

- **MJPEG stays a permanent, supported endpoint.** Clients can request resolution, frame
  rate, and JPEG quality (e.g. `?width=320&fps=10&quality=70`) so small displays such as
  ESP32-based control panels receive a stream they can decode. A single-frame JPEG
  snapshot endpoint is also provided.
- **Embedded clients do not need video.** An ESP32 control panel should work with the
  REST and WebSocket API alone; video is optional for it.
- **WebRTC** is negotiated over the existing authenticated API (no separate signaling
  server), LAN-only by default. Studio uses WebRTC when available and falls back to MJPEG.
- **Overlays are data, not pixels.** Crosshairs, detected features, and vision results are
  sent as vector data over the event socket and drawn by the client, so they stay sharp
  at any stream quality and work with both transports.
- **Frame timestamps** are attached to frames on both transports so clients can line up
  video with machine position and overlay data.
- When no client is watching a stream, the core stops encoding it.
- **Endpoints (built in M1):** `GET /api/v1/cameras`, `.../{id}/snapshot.jpg`, and
  `.../{id}/stream.mjpg` with `width`, `fps`, `quality`, and `frames` parameters. Each
  JPEG carries `X-Timestamp` and `X-Frame` headers. Media endpoints also accept the token
  as a `token` query parameter, since `<img>` tags cannot send headers. Each camera
  captures once however many clients watch, and stops a few seconds after the last
  leaves. The simulator renders synthetic views from the machine state (bed, a demo board
  with fiducials, feeder tape, nozzles and held parts) that match the machine geometry,
  so vision can be tested against known truth.

## 7. Core subsystems

### 7.1 Machine model

A declarative, data-driven model, loaded from config:

- **Axes** (X, Y, Z per nozzle or shared, rotation per nozzle), with limits, safe Z, and
  backlash compensation settings.
- **Head** containing **nozzles**, **cameras** (top/"down-looking"), and **actuators**.
- **Fixed cameras** (bottom/"up-looking").
- **Nozzle tips** with sizes, vacuum thresholds, and tool-changer positions.
- **Feeders** (see §7.4).
- **Actuators and sensors**: vacuum pumps, valves, lights, vacuum pressure sensors.
- **Transforms**: camera-to-nozzle offsets, camera pixel scale (units per pixel at a given
  Z), lens distortion calibration, nonsquareness/skew compensation.

### 7.2 Drivers

A `MotionDriver` interface with implementations:

| Driver | Priority | Notes |
|---|---|---|
| **Simulator** | 1.0, first | Simulated motion and synthetic camera images. Required for CI and contributor development without hardware. |
| **Marlin (LumenPnP)** | 1.0 | G-code over USB serial, `ok`-based flow control, position reports, vacuum sensor reads, RS-485 passthrough for Photon feeders. The machine-specific G-code lives in the machine profile's `[marlin]` table, with named firmware variants. A fake LumenPnP firmware (`fake-marlin`) lets the real driver run without hardware. |
| Klipper (via Moonraker API) | Post-1.0 | |
| grblHAL / Smoothieware | Post-1.0 | |

Driver responsibilities: connect/handshake, queue commands with flow control, report
position and status, wait for motion completion, read sensors, issue immediate stop.

### 7.3 Job engine

- **Import** placement (centroid) files from KiCad, Eagle/Fusion, Altium, and generic CSV
  with column mapping. Import BOMs to link placements to parts.
- **Board setup**: board origin and rotation via fiducials (2+ fiducials; affine fit with
  3+), panels/arrays, and bottom-side placements.
- **Planner** orders placements to minimize travel and nozzle-tip changes, and to use
  multiple nozzles in parallel (pick N, align N, place N).
- **Executor** runs the plan as a state machine with pause, resume, single step, skip, and
  retry. A run can start in step mode: it finds the board, then pauses before the first
  part, and each step places one batch. **Check fiducials** finds a job's board and shows
  the fit, each fiducial's offset, and a button to look at each one, placing nothing (it
  works with every placement turned off). Every step emits events. Errors (mis-pick, vacuum failure, vision failure) stop or
  retry according to policy and ask the operator through the API when needed.
- **History**: each run records placements, timings, vision results, and errors in SQLite.

### 7.4 Feeders

A `Feeder` interface: `prepare()`, `feed()`, `pick_location()`, plus optional vision
correction. Implementations:

- **Photon feeders (LumenPnP)**: addressed over RS-485 through the motherboard. Support
  discovery, address assignment, feed, and status. 1.0. Done so far:
  - Slots come from the profile's feeder banks (pick location, pitch, tape direction,
    and each slot's bus address). The owner's feeder setup (`feeders.json` in the data
    directory) holds which part each feeder holds, by feeder ID, so the part follows the
    feeder and its reel to another slot, and each slot's pick correction.
  - Pick corrections are taught by eye, or found from the feeder's alignment fiducial,
    which sits at a fixed place on the feeder's design (in the profile; it can also be
    measured once by eye): every slot is calibrated by finding its feeder's fiducial
    with the top camera.
  - **Rails are calibrated first**, once, at setup: where each rail really is depends
    on how the machine was assembled. The core finds the fiducials of the two feeders
    farthest apart on a rail (the feeders already mounted; only if a rail has fewer
    than two, far enough apart, does it ask for feeders at its ends). Fitting the
    profile's rail to them (a shift, a turn, and a stretch) places every slot between.
    It searches about 10 mm around where it expects each fiducial; if that fails, it
    asks the operator to jog the camera onto it. Feeder corrections that all come out
    alike later say the rail has moved.
  - Planned next: at the start of a job, the feeders are scanned, and on the first pick
    from each, its fiducial is found (near where the calibrated rail puts it) and the
    pocket found from the sprocket holes, so feeders the job does not use cost nothing.
    The fiducial's place belongs to the feeder (its ID), not the slot, since each
    feeder's fiducial board sits a little differently. After that first pick an
    **auto** pick correction keeps picks centered with the bottom camera and goes back to
    the top camera only when a pick fails, or the bottom camera's misses grow (a feeder
    stepping unevenly, which learning cannot follow).
  - Feeders do not step exactly and their errors add up, so the pick pocket can be found
    at pick time from the tape's sprocket holes: EIA-481 fixes each pocket's place
    relative to them (2 mm along the tape, F across). Errors over 2 mm along the tape
    cannot be told from the next pocket.
  - Or the bottom camera keeps picks centered instead (suggested on the LumenPnP
    Discord): how far off the nozzle's center it sees each part says where that part
    really lay when it was picked, and each feeder (slot, strip, or tray) learns the
    offset from where its setup puts its parts: an even average of its first four picks,
    then each new pick counting for a quarter. Picks more than 1.5 mm off center are
    not learned from, and the offset is capped at 3 mm. It saves the top camera's trip
    before each pick, but cannot follow an error that changes from part to part, such
    as a feeder that steps unevenly. Teaching, calibrating, or moving a feeder, or a
    different Photon feeder in the slot, starts it again.
  - Which one is used is the owner's choice (`machine.pick_correction` in the config:
    `top`, `bottom`, or `off`), and each run or single pick can choose again. The bottom
    camera learns, and keeps score of how far off center each way's picks have been,
    whichever is chosen, so the two can be compared on the machine. Trays have no
    sprocket holes, so they use what it learned unless the choice is `off`.
  - The simulator has Photon feeders on its bus (and the fake firmware answers `M485`),
    with tape, parts from the library, a fiducial, a fixed error per slot, and feeders
    that step 1% long, so all of the above runs without hardware.
- **Strip and tray feeders** (fixed feeders: cut tape on a fixture, or a grid of parts in
  a tray), supported. Each is kept in the feeder setup by ID (T1, T2, ...) with its
  part, the first part's location, the step along a row and from row to row, the
  number of parts and rows, a pick height, and how many parts have been taken. Parts
  are taken along each row, then row by row; every pick, taken or not, moves on to the
  next part. The first part, the end of the first row, and the last part are taught
  with the top camera. A strip's parts take their angle from the part's tape, and its
  next pocket can be found from its sprocket holes (the camera looks at the holes, since
  on wide tape they are out of view from the pocket); a tray's parts have one angle.
  The planner uses feeder slots first, warns when a strip or tray has fewer parts than
  the job takes from it, and lets a part on tape too wide for the feeders come from a
  strip. An empty one pauses the run until the operator marks it refilled, with the nozzles
  raised and the head moved to the owner's park location (or, without one, to the back
  of its travel) so it is out of the way. The machine
  map shows each with the parts left.
- Push-pull/drag feeders and tube feeders. Post-1.0.

### 7.5 Vision

- **Pipelines** are defined as data (a list of stages with parameters), editable in the
  GUI with live preview, and stored per part/package/feeder. Users can also write a
  pipeline stage in Python.
- **Built-in pipelines**: fiducial locate, bottom-vision part alignment (rectangle/
  body/lead fit), sprocket hole detection, nozzle-tip runout calibration, camera
  calibration (checkerboard/ChArUco).
- **Fiducial locate** sweeps many thresholds and keeps the round blobs found at several.
  It ranks them by how many levels found them and, when the fiducial's size is known
  (from its footprint, else 1 mm), how close they are to that size. Only among the
  strongest does nearness to the camera's center decide. A round silkscreen letter near
  a fiducial (the "D" of a "FID1" label fooled the first version on a real board) is
  found at fewer levels and is the wrong size.
- **Acceleration abstraction**: stages operate on an image type that maps to `cv2.UMat`
  (OpenCL) when available and to NumPy arrays otherwise. CUDA support is a later,
  optional backend. CPU must always work and is the reference for tests.
- **Debuggability**: every pipeline run can return intermediate images and numeric
  results over the API, so the GUI can show each stage.
- **Test corpus**: a collection of real LumenPnP camera images with expected results,
  used in CI to catch regressions. Flight recordings (below) are how real images arrive.
- **Flight recorder** (done): on request, or for every run, the core keeps every frame
  vision looked at (lossless PNG) with the vision function, its arguments, and its
  answer, a JSON-lines timeline of what the machine did (the event stream, the log,
  moves, vacuum readings, picks, alignments, learning, placements, run progress), and a
  snapshot of the setup (the config without its token or data folder, the profile, the
  feeders, the parts in use, the job). Recordings are kept in the data directory, the
  newest ten and at most 5 GB by default, and download as one zip from the Studio.
  `picnicd --replay` runs today's vision on a recording's frames and reports every answer
  that changed, so a user's recording becomes a regression test. Recording never stops
  the machine: what cannot be written is logged and dropped. A recording holds photos of
  the user's boards and, for a run, the job, and the guide says so before it is sent.
  Next: a replay camera for the simulator, so a run can be driven by recorded frames,
  and an optional upload to the project (see the guide).

### 7.6 Configuration and data

- Machine config: human-readable text files under a config directory, validated by
  Pydantic, versioned with automatic migrations. Easy to diff and back up.
- Part, footprint (OpenPnP: package), and feeder libraries: SQLite, with import/export to
  text. Imports merge: new entries are added and empty values filled in, but a value the
  library already has is kept and the difference reported, so values tuned on the
  machine survive re-importing a board.
- Jobs: one JSON file per job, holding the board, panel, side up, and location.
- Calibration results are stored separately from hand-written config so recalibration does
  not rewrite user edits.

### 7.7 Calibration from scratch

The built-in LumenPnP profile was drawn from one machine's OpenPnP configuration, so its
per-machine values (nozzle offsets, the bottom camera's position and focus, feeder pick
locations, heights) are close but not exact for any other machine. Without an OpenPnP
configuration to start from, they are found on the machine. The profile's values are where
each search starts; its design facts (axes, travel, feeder geometry, the datum board)
are not calibrated.

Each calibration below is done by the machine where it can be (§8.2): it moves, looks,
measures, checks its own result against the last one and the profile's, and asks the
operator, in a dialog, only for what needs a person. Results are kept apart from the
config (§7.6) with when they were measured, so the Studio can show what is stale (after a
crash, a nozzle tip change, or a camera knocked).

In order, since each needs the ones before it:

| # | Calibration | How | Operator | Status |
|---|---|---|---|---|
| 1 | **Machine frame**: homing, Z reading 0 with the nozzles level, which way each axis and nozzle moves, which output is each pump and valve, the vacuum sensors' format | Home and check Z against the profile. Jog over the bed and watch the top camera's picture move to find the axes' directions. Switch each output and watch the vacuum readings to tell pumps from valves. The probe reads the firmware and sensor format. | Watch the first homing and first moves with a hand on the power switch, until the machine has checked itself | Z check done. Directions and outputs are checked by hand (the bring-up checklist) today. |
| 2 | **Top camera orientation**: flips and rotation | Jog the head by known distances and track the bed's texture or a fiducial; the direction and angle of the motion give the flips and the rotation | None | Planned. Set by hand in the config today. |
| 3 | **Homing fiducial** (the datum board's dot) | Search around the profile's position after homing (the datum board's place varies with assembly) | Confirm, once, that the mark found is the datum board's center dot (a dialog with the picture) | Visual homing done. The search and confirmation are planned. |
| 4 | **Top camera scale and lens**: millimeters per pixel at board height and at feeder height, and lens distortion | Move a fiducial to many places in the view by known distances | None | Scale at one height done; heights and distortion planned |
| 5 | **Bottom camera**: where it is, its focus height, scale, and rotation | Bring a nozzle tip over it, center the tip by vision, step Z for the sharpest image, and step X and Y by known distances | Fit nozzle tips | Planned. Position and focus come from the profile today. |
| 6 | **Nozzle offsets**: where each nozzle tip is from the top camera | With the round calibration token a LumenPnP ships with (a tiny round PCB, or a paper hole punch) on the datum board: find it with the top camera, pick it with the nozzle, turn it, put it down, and find it again. Where it lands against where the nozzle was sent gives the offset, and how it moves as it turns separates the tip's runout from the offset. Repeat until it stops changing. | Put the token on the datum board | Planned. From the profile today. |
| 7 | **Nozzle runout**: how far each tip's center wanders as it turns | Turn the tip over the bottom camera and fit a circle to the tip's center | None (again after each tip change) | Planned |
| 8 | **Heights**: the board's top surface, the top of parts in each feeder bank, and each strip and tray | Lower a nozzle slowly with the vacuum on: when the open tip seals against a flat surface the vacuum jumps, which marks contact (OpenPnP finds heights this way). Part heights from the library are added. | Measure by jogging down where the vacuum cannot tell (a pocketed tray) | Planned. Jogged down by hand today (strips and trays) or from the profile. |
| 9 | **Vacuum levels** for each nozzle tip: open, sealed, and holding a part | Read the vacuum open, and holding a part during step 6 | None | Planned. One threshold from the profile today. |
| 10 | **Feeder rails and pick locations** | Rails first: find the fiducials of the two feeders farthest apart on each rail and fit the rail to them, which places every slot between. Then each feeder's fiducial gives its pick location (the design offset), and the pocket is found by its sprocket holes; planned, on the first pick from each feeder in a job. Picks are then kept centered by the top or bottom camera (§7.4). | Load the feeders. Jog the camera onto a rail's end fiducial only if the search does not find it; put feeders at a rail's ends only if it has fewer than two far apart. | Rail calibration being built; slots from fiducials done |
| 11 | **Strips and trays** | Teach the first part, the end of the first row, and the last part | Center the camera on each one; give the part count and pick height (step 8 will measure the height) | Done |
| 12 | **Each board** | Its fiducials, at the start of every run (or **Check fiducials**) | Fasten the board where the map shows it | Done |

Physical preparation the machine cannot do for itself: the datum board fitted, nozzle tips
in, feeders on the rails with reels loaded, the calibration token on the datum board for
step 6, and boards fastened down.
Everything else should come down to a sequence the operator starts and then answers
dialogs for.

## 8. GUI — PicnicPlace Studio

### 8.1 Technology

- **TypeScript**, **React**, built with **Vite**.
- 2D board/machine view on canvas or WebGL (e.g. PixiJS); 3D view optional later.
- Served by the core for browser use. Optionally packaged as a desktop app with **Tauri**,
  which also gives a native window, menus, and file dialogs.
- API client generated from the core's OpenAPI schema.

### 8.2 Design direction

Inspired by OrcaSlicer: a clean, dense-but-calm layout, dark and light themes, a single
accent color, flat icons, and a large central canvas with settings in a side panel.

Proposed top-level workspaces (tabs across the top, as in OrcaSlicer's Prepare / Preview /
Device):

| Workspace | Contents |
|---|---|
| **Prepare** | Import boards, edit placements, assign parts/packages, set up panels and fiducials. Central canvas shows the board with placements. |
| **Feeders** | Feeder layout on a machine-bed view, Photon feeder discovery, part assignment, pick-location teaching. |
| **Run** | Job progress, live camera views, placement list with status, pause/step/stop, error prompts. |
| **Machine** | Machine view (§8.3), camera views in the sidebar, jog controls, actuators, guided calibration wizards, machine settings. |
| **Vision** | Pipeline editor with live preview of each stage. |

#### Operating and engineering are separate

Running a machine and preparing a job are different work, often by different people.
The interface is split along that line:

- **The operations view**, for the operator at the machine, holds the minimum needed to
  run it: the job controls (load a job, start, pause, step, stop, and the prompts a run
  raises), the camera views, the machine view, and what is loaded on the machine and how
  much of it is left. Large, touch-friendly controls; nothing that changes a job's
  design.
- **The engineering tools**, a separate page (or site), hold everything for creating a
  job: importing boards and panels, placements, parts and footprints, fiducials, and
  vision settings. They produce a single **job file** that holds everything needed to
  run the job, so what the engineer prepared is exactly what the operator runs, and a
  job can be moved between machines or kept with the board's design files.
- **Inventory on the machine.** When the operator loads a reel or a tray, they enter how
  many parts it holds. The core takes one off each time it feeds (or, for a strip or
  tray, each time a part is taken), keeps the count with the feeder (a Photon feeder's
  count follows it to another slot, like its part), and shows what is left on the
  machine view. The run asks for a refill when one runs out. Strips and trays already
  count their parts (§7.4); reels on feeders are next.
- **An inventory check before every run.** Starting a run first compares what the job
  takes with what is loaded. If anything is short (a part with no feeder or tray on the
  machine, or one with fewer parts left than the job needs), the operator sees a dialog
  listing each short part: its part number, how many the job needs, how many are
  loaded, and where. They choose one of:
  - **Cancel**, and load what is missing before starting;
  - **Run, and pause when a part runs out**, to load more then (the run asks for it,
    as for any empty feeder);
  - **Run without the missing parts**, leaving those placements unplaced. This one asks
    again ("Really leave out these N placements? The boards will be missing …"), since
    it makes boards that are not complete.
  A run with nothing short starts without asking.
- **When the machine needs help, it asks.** Whatever stops the machine and needs a
  person (a feeder out of parts, a part that will not pick, a fiducial not found, a
  nozzle tip to change) opens a dialog over the operator's screen. It says plainly what
  is wrong, what the operator needs to do, step by step, and what will happen after
  (for example, "Load a new reel of 10 kΩ 0603 in slot 12, enter its count, then press
  Continue; the run carries on from the next part"). Its buttons are the choices that
  make sense there (Continue, Skip this part, Stop the run), and the camera view or
  machine map shows where to look. The operator should never have to read a log or
  find a status line to learn why the machine stopped.
- **Calibration is an advanced part of the operations view**, not a separate tool. It
  is as hands-off as the machine can make it: the machine moves, looks, measures, and
  checks its own results, and asks with the same kind of dialog only when it needs a
  person (put the calibration board down, fit a nozzle tip, confirm what the camera
  shows). Teaching feeders works the same way.

[OPEN] The job file's format (one archive with
the boards, placements, parts, footprints, and vision settings, or a single TOML or JSON
document), and whether the engineering tools need a core at all, or can run without a
machine as a static site.

The workspaces below are today's Studio; they will be regrouped into the two views.

Design principles:

- **Guided first, advanced available.** Calibration and setup are wizards with
  explanations and pictures; raw settings are behind an "Advanced" toggle, as OrcaSlicer
  does with its simple/advanced modes.
- **Live state everywhere.** Machine position, connection state, lease holder, and E-stop
  are always visible in a status bar.
- **Touch friendly** on the Run and Machine workspaces, for tablets mounted near the
  machine.
- Design tokens (color, spacing, type) in one place so themes are easy to adjust.

### 8.3 Machine view

The center of the Machine workspace is the **machine view**: a scale overhead map of the
machine, with a **head inset** in its lower-right corner. Live camera feeds sit in the
sidebar beside it, and the axis positions are in the status bar.

#### Overhead view

- Drawn to scale in machine coordinates (X right, Y up), with zoom and pan. The machine
  coordinates under the cursor are shown.
- Shows:
  - the travel limits (soft limits) and machine bed outline
  - boards, with their placements as markers colored by status
  - feeders (Photon slots on the LumenPnP) and their pick locations
  - the bottom camera, with its field of view
  - the homing fiducial
  - nozzle tip holders and the discard location
- The head is a crosshair at the current position, with the top camera's field-of-view
  rectangle and each nozzle's offset drawn around it. Its live position comes from the
  `machine.state` events.
- **Click to move**, only while motion is enabled. A modifier key or a confirm step is
  required so a stray click or tap cannot move the head. Moves to the target at safe Z.

#### Head inset

A side view of a generic head, floating in the lower-right corner of the overhead view.
It can be collapsed.

- Each nozzle's height, drawn to scale against the safe-Z line. On the LumenPnP v4 both
  nozzles share one Z motor and move in opposite directions, and the drawing shows that.
- Each nozzle's rotation as a small dial, the fitted nozzle tip, and whether a part is
  held.
- **Status lights along the edge**, generated from the machine profile rather than hard
  coded, so the panel matches each machine's actual I/O:
  - **Inputs** are read-only lights: endstops/home sensors, safe Z, and others the
    profile declares.
  - **Safe Z** is a solid light when sensed by a physical input (§5.2) and an outlined
    light when computed.
  - **Vacuum** per nozzle is a small bar showing the pressure reading with the pick
    threshold marked, plus a lit state. This makes weak picks visible before they fail.
  - **Outputs** (pump, valves, ring lights) look different from inputs and can be
    toggled with a click in manual mode, when the client holds the control lease.

#### Core support needed

- A **machine description** endpoint: bed size and limits, camera and fiducial positions,
  nozzles and their offsets, tip holders, feeders, and the list of inputs and outputs with
  their kinds (digital, analog, sensed or computed).
- Input and output states in the `machine.state` events.
- The simulator provides all of the above with simulated sensor values, so the machine
  view can be developed and tested without hardware.

### 8.4 CLI

A `picnic` command line client for scripting and headless use: connect, status, home,
jog, run a job, export logs. Uses the same API.

## 9. Platform support

**Decided: the 1.0 core supports Linux only.**

| Platform | Core | GUI (browser) | GUI (desktop) |
|---|---|---|---|
| Linux x86_64 | Supported (reference) | Supported | Supported |
| Linux ARM64 (Raspberry Pi 5, other SBCs) | Supported | Supported | Best effort |
| macOS (Apple silicon) | Development only (simulator) | Supported | Not in 1.0 |
| Windows 10/11 x86_64 | Not in 1.0 | Supported | Not in 1.0 |

- **Supported:** tested in CI and on real hardware before release.
- **Development only:** the core runs against the simulator so contributors can develop on
  a Mac, but real hardware is not supported or tested.
- The browser GUI works on any OS, since it only needs a modern browser.

Camera capture and serial port naming are kept behind interfaces, which is where most
porting work will be when macOS and Windows support is added.

## 10. LumenPnP target

To verify against the actual machine and Opulo's published configuration:

- Firmware: Marlin-based on the LumenPnP motherboard, G-code over USB serial.
- Two nozzles on a shared head, each with Z and rotation.
- Top camera on the head; bottom camera fixed under the head path. Both USB UVC cameras.
- Vacuum pump(s), valves, and vacuum pressure sensors per nozzle.
- Photon feeders on an RS-485 bus via the motherboard.
- Ring lights on both cameras.

**Likely second target: Quad 4c.** An older industrial machine the project lead plans to
convert. It has a physical safe-Z input. Driver and machine-profile interfaces must not
assume LumenPnP-only behavior.

PicnicPlace should ship a **LumenPnP machine profile** so a new user selects their machine
version and goes straight to guided calibration.

**Reference machine: LumenPnP v4.1.** The first machine profile targets v4.1 (PCB
staging plates, bottom camera at the center). v4.2 (metal plates, camera near one end)
needs its own profile. Other versions are community supported. The test machine was built by Opulo around October 2026, so it
should have recent stock firmware.

The Marlin driver reads the firmware name and version (`M115`) on connect, records it in
the machine state, and warns when the version is not in the known-tested list instead of
refusing to connect.

## 11. Milestones

| Milestone | Scope |
|---|---|
| **M0 — Skeleton** | Repo layout, tooling, CI, `picnicd` with health endpoint, simulator driver, event socket, GUI shell with theme and status bar. |
| **M1 — Manual control** | Machine description API and I/O state; machine view with head inset (built first, against the simulator); connect to LumenPnP, home, jog, actuators, vacuum readings, safe-Z interlock, MJPEG camera streams in the GUI, control lease, E-stop. |
| **M2 — Calibration** | Camera scale and lens calibration, camera-to-nozzle offsets, nozzle runout, bottom camera position. Guided wizards. WebRTC video. Started: fiducial finder, visual homing (after `G28`, center the top camera on the homing fiducial and correct X/Y with `G92`), and a camera scale measurement, all tested against the simulator, whose homing has a realistic error for visual homing to find. |
| **M3 — Single placement** | Parts/packages library, Photon feeder support, pick, bottom vision alignment, place at a manually entered location. Started: the parts library (footprints, parts with height and its source, tape packaging after EIA-481 with rotation in tape and pin-1 quadrant), merge-only imports, OpenPnP library import, and a library editor in the Studio. Photon feeders: discovery and initialization, part assignment by feeder ID, feeding, simulated feeders with drawn tape, slot pick locations taught by eye or found from each feeder's fiducial, and pockets found from the tape's sprocket holes; a Feeders workspace and a bring-up checklist in the Studio. Single placement: pick (feed, find the pocket, pick at the tape's part height, check the vacuum), align on the bottom camera (the part's offset and angle on the nozzle), and place at a given location and angle (with a part-off check), one step at a time or all three, in the simulator to within 0.05 mm; camera captures for tuning vision on real images. |
| **M4 — Jobs** | Import KiCad/CSV placements, fiducial board alignment, planner, executor with pause/step/stop, run history. Boards must lie inside the profile's placement area (where every nozzle can reach, on the staging plates) and clear of its keep-outs; import warns about any placement a nozzle cannot reach. Started: KiCad board import (footprints with pads, part numbers from the board or the schematic's BOM), panels recovered from a KiKit panel file, jobs placed on the bed with reach, camera, and keep-out checks and a search for a place that fits (parts within every nozzle's reach; empty rails may lie outside it), saved as job files, and a Prepare workspace. Runs: a planner that is also a dry run against the loaded feeders (blocked placements and why, parts only some nozzles reach, tape too wide for the feeders), board alignment from up to three fiducials (a rigid fit), batches with a part on every nozzle that can take one, lowest parts first, retries for empty picks, discard and retry after a failed alignment, pause, step, resume, stop, and skip, and a Run workspace; in the simulator a whole board lands within 0.06 mm of where the misplaced board wants its parts. Strips and trays held on the bed as feeders, with refills (the run pauses and parks the head). Picks kept centered by the top camera (pockets from sprocket holes) or the bottom camera (each feeder learns where its parts really lie from earlier picks), the owner's choice. A flight recorder for sending real frames and timelines to developers, with vision replay. Run history is to come. |
| **M5 — 1.0 polish** | Installer/packaging, docs. The operations view and the engineering tools split (§8.2), the single job file, and parts counted on reels as well as strips and trays. Done early: strip and tray feeders (M4), OpenPnP library import (M3). |

## 12. Repository layout

```
picnicplace/
  core/            Python package: picnicd, drivers, vision, job engine, CLI, and the
                   built-in machine profiles (core/src/picnicplace/profiles/); its
                   tests and their sample boards in core/tests/
  studio/          TypeScript GUI
  docs/            The website: spec, guides, code map, generated OpenAPI document
  AGENTS.md        Rules and commands for coding agents (CLAUDE.md points to it)
```

The [code map](developers/code-map.md) describes every module.

## 13. Open questions

1. ~~License~~ **Decided: MPL-2.0** (see §14).
2. ~~Config format~~ **Decided: TOML.**
3. ~~GUI framework~~ **Decided: React.**
4. ~~Video transport~~ **Decided: MJPEG and WebRTC** (see §6.5).
5. ~~Auth~~ **Decided:** single shared secret for 1.0; local-only motion planned next;
   roles later (§6.3).
6. ~~Client disconnect~~ **Decided:** motion requires a connected client; jobs pause, not
   end, when none are connected (§5.3).
7. Exact LumenPnP v4 firmware version: read it from the machine (`M115`) at M1.
8. ~~Platforms~~ **Decided:** Linux only for 1.0 (§9).
9. ~~Part on nozzle at pause~~ **Decided:** hold it (§5.3).

## 14. Licensing (decided)

**License: Mozilla Public License 2.0 (MPL-2.0)** for all PicnicPlace code.

Goals: commercial users can adopt and integrate PicnicPlace without fear, while
improvements to PicnicPlace itself stay open.

- **File-level copyleft.** Anyone who distributes a modified version of a PicnicPlace
  source file must publish that file's source under MPL-2.0. Closed-source edits to the
  core are not allowed.
- **Proprietary additions are allowed in separate files.** A vendor can ship its own
  driver, feeder, or vision stage in its own files under its own license. This is an
  intentional tradeoff to keep commercial users comfortable.
- **Network clients are independent works.** Software that only talks to the core over
  the API (custom GUIs, MES or ERP integrations) can use any license.
- **Patent grant** protects users and contributors.
- **GPL compatible.** MPL-2.0 code can be combined into GPL projects.

Supporting policies:

- **Trademark.** The PicnicPlace name and logo are reserved by a written trademark policy.
  Forks may use the code but not the name, so a fork cannot present itself as the official
  project.
- **Contributions** use the Developer Certificate of Origin (`Signed-off-by` on
  commits), not a CLA. Copyright stays with the contributors, which keeps any later
  relicensing to a closed license impractical.
- **No OpenPnP code.** OpenPnP is GPL-3.0. Its code must not be copied or ported line by
  line. Reimplementing published algorithms and reading its file formats is fine.

Alternatives considered:

| License | Why not chosen |
|---|---|
| MIT / Apache-2.0 | Allows closed-source forks of the core. |
| GPL-3.0 | Many companies avoid it for products. Its copyleft also isn't triggered when software is only offered over a network, which matters for a network-first design. |
| AGPL-3.0 | Strongest protection, but many companies ban it outright. |
| LGPL-3.0 | Written around C-style linking, so it is ambiguous for Python and web code. MPL is clearer. |
| BSL / source-available | Not open source; would discourage contributors. |
