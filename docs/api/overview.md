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
| WebSocket at `/api/v1/events` | Live state, operation progress, and presence. A client must hold this connection to move the machine. |
| MJPEG over HTTP | Camera streams and snapshots at the size, rate, and quality the client asks for |

## Endpoints today

| Method and path | What it does |
|---|---|
| `GET /api/v1/health` | Is the core running |
| `GET /api/v1/machine` | Live machine state: position, inputs, outputs, safe Z, E-stop |
| `GET /api/v1/machine/description` | The machine profile: axes, nozzles, cameras, plates, I/O |
| `POST /api/v1/machine/connect` | Connect to the machine's controller |
| `POST /api/v1/machine/home` | Home, then visual home if the profile enables it |
| `POST /api/v1/machine/move` | Move to a position, traveling at safe Z |
| `POST /api/v1/machine/jog` | Jog one axis by a distance |
| `PUT /api/v1/machine/outputs/{id}` | Switch a pump, valve, or light |
| `POST /api/v1/machine/visual-home` | Center the top camera on the homing fiducial and correct the position |
| `POST /api/v1/machine/camera-scale` | Measure the top camera's scale |
| `POST /api/v1/machine/estop` | Emergency stop; any client may call it |
| `POST /api/v1/machine/reset` | Clear a latched emergency stop |
| `GET`, `POST`, `DELETE /api/v1/lease` | See, take, or release the control lease |
| `GET /api/v1/operations/{id}` | Status of a long-running command |
| `GET /api/v1/presence` | Connected clients |
| `GET /api/v1/cameras` | Cameras and whether they are streaming |
| `GET /api/v1/cameras/{id}/snapshot.jpg` | One JPEG frame |
| `GET /api/v1/cameras/{id}/stream.mjpg` | A live MJPEG stream |
| `GET`, `PUT`, `DELETE /api/v1/library/parts/{id}` | Parts (and `GET /library/parts` to list) |
| `GET`, `PUT`, `DELETE /api/v1/library/footprints/{id}` | Footprints (and `GET /library/footprints`) |
| `GET /api/v1/library/export`, `POST /api/v1/library/import` | The library as one JSON document |
| `POST /api/v1/library/import/openpnp` | Merge OpenPnP `packages.xml` and `parts.xml` |
| `POST /api/v1/boards/import/kicad` | Read a KiCad board (and panel, and BOM) into a board, its library entries, and a new job |
| `GET /api/v1/jobs`; `GET`, `PUT`, `DELETE /api/v1/jobs/{id}` | Saved jobs |
| `POST /api/v1/jobs/layout` | Where a job's placements land on the machine, and what stops it |
| `POST /api/v1/jobs/find-location` | A place on the bed where a job fits |
| `GET /api/v1/feeders` | Feeder slots, the feeder found in each, and where to pick |
| `POST /api/v1/feeders/scan` | Find and initialize the feeders on the bus |
| `POST /api/v1/feeders/slots/{id}/feed`, `…/identify` | Feed a feeder, or flash its light |
| `PUT`, `DELETE /api/v1/feeders/slots/{id}/pick` | Teach a slot's pick location (at the camera), or forget it |
| `POST /api/v1/feeders/slots/{id}/pocket` | Find the pick pocket from the tape's sprocket holes |
| `POST /api/v1/feeders/slots/{id}/fiducial`, `POST /api/v1/feeders/calibrate` | Measure the feeder fiducial's offset; calibrate slots from their fiducials |
| `PUT`, `DELETE /api/v1/feeders/assignments/{feeder_id}` | Which part a feeder holds |
| `GET`, `PUT /api/v1/bring-up` | The bring-up checklist's progress and findings |

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
