# PicnicPlace — Specification (Draft 0.1)

> Status: **Draft**. Sections marked **[OPEN]** need a decision before work in that area starts.
> Last updated: 2026-11-07

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
5. **Cross platform core** on Linux, macOS, and Windows, with Linux as the reference
   platform (see §9).
6. **Approachable for contributors.** A popular language, conventional tooling, a
   simulator so most work needs no hardware, and good tests.

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
| Cross platform (Linux, macOS, Windows) | Must |
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
- **Client disconnect is safe.** If the controlling client disconnects during manual
  jogging, motion stops. A running job continues (it is owned by the core) unless
  configured otherwise.
- **Emergency stop** is a dedicated, highest-priority API call and WebSocket message that
  bypasses the command queue and sends the firmware's immediate stop (e.g. `M112` or
  realtime reset). A physical E-stop remains the primary safety device; the software stop
  is a convenience.
- **Control lease.** Many clients may observe; only one holds the **control lease** at a
  time and can move the machine. Leases can be requested, handed over, or forcibly taken
  with confirmation.

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
- Roles: `observer` (read only, view cameras), `operator` (run jobs, jog), `admin`
  (change config, manage users). **[OPEN]** Is a role system wanted for 1.0, or is a
  single shared token enough?

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
| **Marlin (LumenPnP)** | 1.0 | G-code over USB serial, `ok`-based flow control, position reports, vacuum sensor reads, RS-485 passthrough for Photon feeders. |
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
  retry. Every step emits events. Errors (mis-pick, vacuum failure, vision failure) stop or
  retry according to policy and ask the operator through the API when needed.
- **History**: each run records placements, timings, vision results, and errors in SQLite.

### 7.4 Feeders

A `Feeder` interface: `prepare()`, `feed()`, `pick_location()`, plus optional vision
correction. Implementations:

- **Photon feeders (LumenPnP)**: addressed over RS-485 through the motherboard. Support
  discovery, address assignment, feed, and status. 1.0.
- **Strip feeders** (cut tape on a fixture) with vision-located sprocket holes. 1.0.
- **Tray feeders** (grid of parts). 1.0.
- Push-pull/drag feeders and tube feeders. Post-1.0.

### 7.5 Vision

- **Pipelines** are defined as data (a list of stages with parameters), editable in the
  GUI with live preview, and stored per part/package/feeder. Users can also write a
  pipeline stage in Python.
- **Built-in pipelines**: fiducial locate, bottom-vision part alignment (rectangle/
  body/lead fit), sprocket hole detection, nozzle-tip runout calibration, camera
  calibration (checkerboard/ChArUco).
- **Acceleration abstraction**: stages operate on an image type that maps to `cv2.UMat`
  (OpenCL) when available and to NumPy arrays otherwise. CUDA support is a later,
  optional backend. CPU must always work and is the reference for tests.
- **Debuggability**: every pipeline run can return intermediate images and numeric
  results over the API, so the GUI can show each stage.
- **Test corpus**: a collection of real LumenPnP camera images with expected results,
  used in CI to catch regressions.

### 7.6 Configuration and data

- Machine config: human-readable text files under a config directory, validated by
  Pydantic, versioned with automatic migrations. Easy to diff and back up.
- Part, package, and feeder libraries: SQLite, with import/export to text.
- Calibration results are stored separately from hand-written config so recalibration does
  not rewrite user edits.

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
| **Machine** | Jog controls, camera views, actuators, guided calibration wizards, machine settings. |
| **Vision** | Pipeline editor with live preview of each stage. |

Design principles:

- **Guided first, advanced available.** Calibration and setup are wizards with
  explanations and pictures; raw settings are behind an "Advanced" toggle, as OrcaSlicer
  does with its simple/advanced modes.
- **Live state everywhere.** Machine position, connection state, lease holder, and E-stop
  are always visible in a status bar.
- **Touch friendly** on the Run and Machine workspaces, for tablets mounted near the
  machine.
- Design tokens (color, spacing, type) in one place so themes are easy to adjust.

### 8.3 CLI

A `picnic` command line client for scripting and headless use: connect, status, home,
jog, run a job, export logs. Uses the same API.

## 9. Platform support

| Platform | Core | GUI (browser) | GUI (desktop) |
|---|---|---|---|
| Linux x86_64 | Tier 1 (reference) | Tier 1 | Tier 1 |
| Linux ARM64 (Raspberry Pi 5, other SBCs) | Tier 1 | Tier 1 | Tier 2 |
| macOS (Apple silicon) | Tier 2 | Tier 1 | Tier 2 |
| Windows 10/11 x86_64 | Tier 2 | Tier 1 | Tier 2 |

- **Tier 1:** tested in CI and on real hardware before release.
- **Tier 2:** tested in CI (unit tests, simulator); hardware testing by the community.

The main cross-platform risks are camera capture (backend differences, exposure control,
camera ordering) and serial port naming. Both are isolated behind interfaces.

## 10. LumenPnP target

To verify against the actual machine and Opulo's published configuration:

- Firmware: Marlin-based on the LumenPnP motherboard, G-code over USB serial.
- Two nozzles on a shared head, each with Z and rotation.
- Top camera on the head; bottom camera fixed under the head path. Both USB UVC cameras.
- Vacuum pump(s), valves, and vacuum pressure sensors per nozzle.
- Photon feeders on an RS-485 bus via the motherboard.
- Ring lights on both cameras.

PicnicPlace should ship a **LumenPnP machine profile** so a new user selects their machine
version and goes straight to guided calibration.

**Reference machine: LumenPnP v4.** The first machine profile targets v4. Other versions
are community supported. **[OPEN]** Record the v4 test machine's firmware version.

## 11. Milestones

| Milestone | Scope |
|---|---|
| **M0 — Skeleton** | Repo layout, tooling, CI, `picnicd` with health endpoint, simulator driver, event socket, GUI shell with theme and status bar. |
| **M1 — Manual control** | Connect to LumenPnP, home, jog, actuators, vacuum readings, MJPEG camera streams in the GUI, control lease, E-stop. |
| **M2 — Calibration** | Camera scale and lens calibration, camera-to-nozzle offsets, nozzle runout, bottom camera position. Guided wizards. WebRTC video. |
| **M3 — Single placement** | Parts/packages library, Photon feeder support, pick, bottom vision alignment, place at a manually entered location. |
| **M4 — Jobs** | Import KiCad/CSV placements, fiducial board alignment, planner, executor with pause/step/stop, run history. |
| **M5 — 1.0 polish** | Strip and tray feeders, installer/packaging, docs, OpenPnP library import. |

## 12. Repository layout (proposed)

```
picnicplace/
  core/            Python package: picnicd, drivers, vision, job engine
  studio/          TypeScript GUI
  cli/             picnic CLI (Python, may live in core/)
  profiles/        Machine profiles (LumenPnP, …)
  docs/            Specs, ADRs, user docs
  testdata/        Vision image corpus, sample boards
```

## 13. Open questions

1. ~~License~~ **Decided: MPL-2.0** (see §14).
2. ~~Config format~~ **Decided: TOML.**
3. ~~GUI framework~~ **Decided: React.**
4. ~~Video transport~~ **Decided: MJPEG and WebRTC** (see §6.5).
5. Roles and multi-user auth in 1.0, or a single token?
6. Should a running job stop when its controlling client disconnects? (Proposed: no,
   configurable.)
7. LumenPnP v4 firmware version to target first.
8. Linux-only for 1.0 with macOS/Windows later, or Tier 2 support from day one as above?

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
