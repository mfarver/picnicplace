import { useCallback, useState } from "react";
import { api } from "./client";
import { hasControl, useCore } from "./core";
import type { Operation } from "./types";
import { useAction } from "./useAction";

/**
 * What a workspace needs to drive the machine: whether it may, and actions that start
 * operations and remember the last one, so its result can be shown.
 */
export function useMachineControl() {
  const core = useCore();
  const { status, machine, operations } = core;
  const { run, pending, error } = useAction();
  const [lastOpId, setLastOpId] = useState<string | null>(null);

  // Another client holds the control lease: this one can watch but not move the machine.
  const controlledElsewhere = !!core.lease?.holder && !hasControl(core);
  const canActuate = status === "open" && !!machine?.motion_enabled && !controlledElsewhere;
  const canMove = canActuate && !machine.busy && !pending;
  const lastOp = lastOpId ? operations[lastOpId] : undefined;

  const track = useCallback(
    async (call: () => Promise<{ data?: Operation; error?: unknown }>) => {
      const op = await run(call);
      if (op) setLastOpId(op.id);
      return op;
    },
    [run],
  );

  const jog = (axis: string, distance: number) =>
    void track(() => api.POST("/api/v1/machine/jog", { body: { axis, distance } }));
  const moveTo = (position: Record<string, number>) =>
    void track(() => api.POST("/api/v1/machine/move", { body: { position } }));

  return {
    core,
    machine,
    controlledElsewhere,
    canActuate,
    canMove,
    run,
    pending,
    error,
    lastOp,
    track,
    jog,
    moveTo,
  };
}
