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
