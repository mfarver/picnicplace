import { Pause, Play } from "lucide-react";
import { api } from "../api/client";
import { hasControl, useCore } from "../api/core";

/**
 * Pause and Resume, always in the top bar. Pause holds the machine before its next move
 * (the move under way finishes) and anyone may press it; Resume, for the client in
 * control (or anyone, when no one is), carries on. There is no software emergency stop here: Marlin acts on one only
 * after the move under way and then hangs until it is reset or power-cycled. The stop for an
 * emergency is unplugging the controller or pressing its reset button, or an E-stop switch
 * that cuts the motors' power.
 */
export function PauseButton() {
  const core = useCore();
  const { status, machine } = core;
  const live = status === "open" && machine !== undefined;
  if (machine?.paused) {
    // With no one in control, resuming takes control (the core gives a free lease to the
    // client that moves the machine); only another client's control stops it.
    const mayResume = hasControl(core) || !core.lease?.holder;
    return (
      <button
        type="button"
        className="pause resume"
        disabled={!live || !mayResume}
        title={
          mayResume
            ? "Carry on from where the machine was paused."
            : "Only the client in control can resume the machine."
        }
        onClick={() => void api.POST("/api/v1/machine/resume")}
      >
        <Play size={16} strokeWidth={2.5} aria-hidden />
        RESUME
      </button>
    );
  }
  return (
    <button
      type="button"
      className="pause"
      disabled={!live || !machine?.connected}
      title={
        "Pause before the next move: the move under way finishes first. In an emergency, " +
        "switch the machine off (or use an E-stop switch that cuts the motors' power)."
      }
      onClick={() => void api.POST("/api/v1/machine/pause")}
    >
      <Pause size={16} strokeWidth={2.5} aria-hidden />
      PAUSE
    </button>
  );
}
