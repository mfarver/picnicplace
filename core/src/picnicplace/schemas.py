"""Models that appear on the wire: REST payloads and WebSocket messages.

Everything here is part of the public API and is exported into the OpenAPI document,
from which the Studio's TypeScript types are generated.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1
"""Version of the WebSocket event protocol. Bumped on breaking changes."""


class WireModel(BaseModel):
    # Fields with defaults (such as the `type` tags) are still always sent, so mark them
    # required in the schema; this gives clients a proper discriminated union.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


# --- State -------------------------------------------------------------------------------


class MachineState(WireModel):
    driver: str
    firmware: str | None = Field(description="Firmware name and version, when known.")
    connected: bool
    connection_error: str | None = Field(
        description="Why the last attempt to connect to the machine failed, if it did."
    )
    homed: bool
    estop: bool = Field(description="An emergency stop is latched; reset before moving.")
    busy: bool = Field(description="A motion operation is in progress.")
    motion_enabled: bool = Field(
        description="Motion commands are currently allowed (connected, client present, no E-stop)."
    )
    axes: list[str]
    position: dict[str, float] = Field(description="Current position per axis, mm or degrees.")
    inputs: dict[str, bool | float] = Field(
        description="Reading per profile input: bool for digital, number for analog."
    )
    outputs: dict[str, bool] = Field(description="State per profile output.")
    speed_scale: float = Field(
        description="Moves run at no more than this fraction of full speed (bring-up mode)."
    )
    warnings: list[str] = Field(
        description="Problems worth showing the operator, such as a firmware mismatch."
    )
    z_safe: bool = Field(
        description=(
            "Every nozzle is at safe travel height. From the safe-Z input when the profile "
            "has one, otherwise computed from the Z position."
        )
    )


class ClientInfo(WireModel):
    id: str
    name: str = Field(description="Name the client gave when connecting, such as 'Studio'.")
    address: str | None = Field(description="Network address the client connected from.")


class LeaseState(WireModel):
    holder: ClientInfo | None = Field(
        description="The client allowed to move the machine, or null if no one holds it."
    )


class PresenceState(WireModel):
    clients: int = Field(description="Clients with a live event-socket connection.")
    present: bool


OperationStatus = Literal["running", "succeeded", "failed", "cancelled"]


class Operation(WireModel):
    id: str
    kind: str
    status: OperationStatus
    error: str | None = None
    message: str | None = Field(
        default=None, description="What the operation found or did, for people."
    )
    started_at: datetime
    finished_at: datetime | None = None


class CameraInfo(WireModel):
    id: str
    label: str
    looking: Literal["down", "up"]
    source: Literal["synthetic", "device", "none"]
    width: int | None = Field(description="Native frame width, when the camera is on.")
    height: int | None
    streaming: bool = Field(description="Frames are being captured for at least one viewer.")
    error: str | None = Field(description="Why the camera is not delivering frames, if so.")


# --- REST --------------------------------------------------------------------------------


class HealthResponse(WireModel):
    status: Literal["ok"] = "ok"
    version: str
    protocol: int
    api: str = Field(
        description=(
            "Fingerprint of the core's OpenAPI document. A Studio built from a different "
            "document comes from a different version of PicnicPlace."
        )
    )


class MoveRequest(WireModel):
    position: dict[str, float] = Field(
        min_length=1, description="Target per axis. Axes left out do not move."
    )
    speed: float | None = Field(
        default=None, gt=0, description="Speed limit in units per second. Defaults to axis max."
    )


class OutputRequest(WireModel):
    on: bool


class JogRequest(WireModel):
    axis: str
    distance: float = Field(description="Relative distance, mm or degrees. Clamped to limits.")
    speed: float | None = Field(default=None, gt=0)


class LeaseRequest(WireModel):
    force: bool = Field(
        default=False,
        description="Take the lease even if another client holds it. Ask the user first.",
    )


class ErrorResponse(WireModel):
    detail: str
    code: str


# --- WebSocket: server to client -------------------------------------------------------


class HelloEvent(WireModel):
    """First message on every connection."""

    type: Literal["hello"] = "hello"
    protocol: int
    server_version: str
    client_id: str
    machine: MachineState
    presence: PresenceState
    lease: LeaseState


class MachineStateEvent(WireModel):
    type: Literal["machine.state"] = "machine.state"
    machine: MachineState


class PresenceEvent(WireModel):
    type: Literal["presence"] = "presence"
    presence: PresenceState


class PresenceLostEvent(WireModel):
    """No client has been connected for the grace period. Running jobs pause."""

    type: Literal["presence.lost"] = "presence.lost"
    grace_period_s: float


class LeaseEvent(WireModel):
    type: Literal["lease"] = "lease"
    lease: LeaseState


class OperationEvent(WireModel):
    type: Literal["operation"] = "operation"
    operation: Operation


class PongEvent(WireModel):
    type: Literal["pong"] = "pong"
    server_time: datetime


class ErrorEvent(WireModel):
    type: Literal["error"] = "error"
    message: str


Event = (
    HelloEvent
    | MachineStateEvent
    | PresenceEvent
    | PresenceLostEvent
    | LeaseEvent
    | OperationEvent
    | PongEvent
    | ErrorEvent
)
ServerEvent = Annotated[Event, Field(discriminator="type")]


# --- WebSocket: client to server -------------------------------------------------------


class PingMessage(WireModel):
    """Heartbeat. Clients send one every couple of seconds; the server answers with pong."""

    type: Literal["ping"] = "ping"


ClientMessage = PingMessage
