import { House, Map as MapIcon, RotateCcw } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { CameraView } from "../components/CameraView";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";

export function MachineWorkspace() {
  const { status, machine, operations } = useCore();
  const { run, pending, error } = useAction();
  const [lastOpId, setLastOpId] = useState<string | null>(null);

  const lastOp = lastOpId ? operations[lastOpId] : undefined;
  const canMove = status === "open" && !!machine?.motion_enabled && !machine.busy && !pending;

  const home = async () => {
    const op = await run(() => api.POST("/api/v1/machine/home"));
    if (op) setLastOpId(op.id);
  };
  const reset = () => run(() => api.POST("/api/v1/machine/reset"));

  return (
    <div className="workspace">
      <aside className="sidebar">
        <CameraView title="Top camera" />
        <CameraView title="Bottom camera" />

        <Panel title="Control">
          <div className="button-row">
            <button type="button" className="button primary" disabled={!canMove} onClick={home}>
              <House size={16} aria-hidden />
              Home
            </button>
            {machine?.estop && (
              <button type="button" className="button" disabled={pending} onClick={reset}>
                <RotateCcw size={16} aria-hidden />
                Reset E-stop
              </button>
            )}
          </div>
          {machine?.estop && (
            <p className="note note-danger">Emergency stop is latched. Reset, then home again.</p>
          )}
          {lastOp && (
            <p className="note">
              {lastOp.kind}: <strong>{lastOp.status}</strong>
              {lastOp.error && ` (${lastOp.error})`}
            </p>
          )}
          {error && <p className="note note-danger">{error}</p>}
          <p className="note text-muted">Jogging and actuators arrive in M1.</p>
        </Panel>
      </aside>

      <div className="canvas">
        <Placeholder
          icon={MapIcon}
          title="Machine view"
          description="The machine bed with the head position, feeders, and boards. Click to move."
          milestone="M1"
        />
      </div>
    </div>
  );
}
