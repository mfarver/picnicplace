import { Camera, House, RotateCcw } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";
import { formatAxis } from "../format";

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
        <Panel title="Position">
          <table className="dro">
            <tbody>
              {machine?.axes.map((axis) => (
                <tr key={axis}>
                  <th scope="row">{axis}</th>
                  <td className="mono">{formatAxis(axis, machine.position[axis])}</td>
                </tr>
              )) ?? (
                <tr>
                  <td className="text-muted">Not connected</td>
                </tr>
              )}
            </tbody>
          </table>
        </Panel>

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

      <div className="canvas camera-grid">
        <Placeholder
          icon={Camera}
          title="Top camera"
          description="Live video from the head camera."
          milestone="M1"
        />
        <Placeholder
          icon={Camera}
          title="Bottom camera"
          description="Live video from the up-looking camera."
          milestone="M1"
        />
      </div>
    </div>
  );
}
