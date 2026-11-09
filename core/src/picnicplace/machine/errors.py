"""Machine errors. Messages are written to be shown to users."""


class MachineError(Exception):
    code = "machine_error"
    http_status = 409


class NoClientPresentError(MachineError):
    code = "no_client"


class EmergencyStopActiveError(MachineError):
    code = "estop_active"


class MachineBusyError(MachineError):
    code = "busy"


class NotConnectedError(MachineError):
    code = "not_connected"


class NotHomedError(MachineError):
    code = "not_homed"


class InvalidTargetError(MachineError):
    code = "invalid_target"
    http_status = 422


class MotionInterruptedError(MachineError):
    code = "interrupted"


class UnknownOutputError(MachineError):
    code = "unknown_output"
    http_status = 404


class ClientRequiredError(MachineError):
    """Motion commands must name the connected client sending them."""

    code = "client_required"


class LeaseHeldError(MachineError):
    """Another client holds the control lease."""

    code = "lease_held"


class ZNotSafeError(MachineError):
    """X/Y motion was requested while a nozzle is below safe Z."""

    code = "z_not_safe"


class SafeZFaultError(MachineError):
    """The safe-Z sensor disagrees with the computed Z position: Z may have lost steps."""

    code = "safe_z_fault"


class ConnectFailedError(MachineError):
    """The machine could not be reached."""

    code = "connect_failed"


class VisionError(MachineError):
    """A vision step could not find what it was looking for."""

    code = "vision_failed"


class FeederError(MachineError):
    """A feeder did not answer, or answered with a failure."""

    code = "feeder_failed"


class FeederEmptyError(FeederError):
    """A strip or tray on the bed has no parts left: the operator must refill it."""

    code = "feeder_empty"


class NotSupportedError(MachineError):
    """The machine or its driver cannot do this."""

    code = "not_supported"
