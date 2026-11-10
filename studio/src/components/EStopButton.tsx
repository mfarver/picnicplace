import { OctagonX } from "lucide-react";
import { api } from "../api/client";
import { useCore } from "../api/core";

export function EStopButton() {
  const { status } = useCore();
  return (
    <button
      type="button"
      className="estop"
      disabled={status !== "open"}
      title={
        "Emergency stop (software). In an emergency, switch the machine off (or use its " +
        "E-stop, if it has one)."
      }
      onClick={() => void api.POST("/api/v1/machine/estop")}
    >
      <OctagonX size={16} strokeWidth={2.5} aria-hidden />
      STOP
    </button>
  );
}
