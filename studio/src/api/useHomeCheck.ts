import { useState } from "react";
import type { MachineProfile, MachineState } from "./types";

/** The open question, for `HomeCheck` to show; null while nothing is asked. */
export interface HomeQuestion {
  /** The machine has a safe-Z sensor, and it reads not safe. */
  sensorUnsafe: boolean;
  onCancel: () => void;
  onHome: () => void;
}

/**
 * Ask the operator to check the nozzles are level before homing (spec §5.2). Homing moves
 * the head across the machine, so a nozzle hanging low can be dragged into things, and on
 * most machines nothing but the operator can see it. A safe-Z sensor that reads safe
 * answers the question, so homing goes ahead without asking.
 *
 * Returns `ask(go)`, which runs `go` once the operator says the nozzles are level, and
 * the question to pass to `HomeCheck`.
 */
export function useHomeCheck(
  description: MachineProfile | null | undefined,
  machine: MachineState | null | undefined,
): [(go: () => void) => void, HomeQuestion | null] {
  const [pending, setPending] = useState<(() => void) | null>(null);
  const sensor = !!description?.safe_z_input;
  const ask = (go: () => void) => {
    if (sensor && machine?.z_safe) go();
    else setPending(() => go);
  };
  const question = pending && {
    sensorUnsafe: sensor,
    onCancel: () => setPending(null),
    onHome: () => {
      setPending(null);
      pending();
    },
  };
  return [ask, question];
}
