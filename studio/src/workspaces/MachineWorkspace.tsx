import { House, Map as MapIcon, Plug, RotateCcw } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { hasControl, useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { useCameras } from "../api/useCameras";
import { useDescription } from "../api/useDescription";
import { useFeeders } from "../api/useFeeders";
import type { Operation } from "../api/types";
import { BringUpPanel } from "../components/BringUpPanel";
import { CalibrationPanel } from "../components/CalibrationPanel";
import { CameraView } from "../components/CameraView";
import { CapturePanel } from "../components/CapturePanel";
import { RecordingsPanel } from "../components/RecordingsPanel";
import { JogPanel } from "../components/JogPanel";
import { LeaseStatus } from "../components/LeaseStatus";
import { Panel } from "../components/Panel";
import { FixedFeederPanel } from "../components/FixedFeederPanel";
import { HeadInset } from "../components/machine/HeadInset";
import { MachineView } from "../components/machine/MachineView";
import { PlacementPanel } from "../components/PlacementPanel";
import { Placeholder } from "../components/Placeholder";

export function MachineWorkspace() {
  const core = useCore();
  const { status, machine, operations } = core;
  // Another client holds the control lease: this one can watch but not move the machine.
  const controlledElsewhere = !!core.lease?.holder && !hasControl(core);
  const { run, pending, error } = useAction();
  const description = useDescription();
  const cameras = useCameras();
  const [feeders, setFeeders] = useFeeders();
  const [lastOpId, setLastOpId] = useState<string | null>(null);

  const lastOp = lastOpId ? operations[lastOpId] : undefined;
  // Successful jogs and moves are visible on the map; only report how homing went, and
  // anything that failed.
  const showLastOp =
    !!lastOp &&
    !machine?.estop &&
    (["home", "feeder_scan"].includes(lastOp.kind) || lastOp.status === "failed");
  // One fixed-height line for results, so buttons below it never shift under the cursor.
  const statusLine = error
    ? { text: error, danger: true }
    : showLastOp
      ? {
          text:
            lastOp.kind === "feeder_scan" && lastOp.message
              ? lastOp.message
              : `${lastOp.kind}: ${lastOp.status}${lastOp.error ? ` (${lastOp.error})` : ""}`,
          danger: lastOp.status === "failed",
        }
      : { text: machine?.homed ? "Ready." : "Home the machine to begin.", danger: false };
  const canActuate = status === "open" && !!machine?.motion_enabled && !controlledElsewhere;
  const canMove = canActuate && !machine.busy && !pending;

  const track = async (call: () => Promise<{ data?: Operation; error?: unknown }>) => {
    const op = await run(call);
    if (op) setLastOpId(op.id);
  };
  const home = () => void track(() => api.POST("/api/v1/machine/home"));
  const jog = (axis: string, distance: number) =>
    void track(() => api.POST("/api/v1/machine/jog", { body: { axis, distance } }));
  const moveTo = (position: Record<string, number>) =>
    void track(() => api.POST("/api/v1/machine/move", { body: { position } }));
  const safeZ = () => {
    if (!description) return;
    const zAxes = new Set(description.nozzles.map((n) => n.z_axis));
    moveTo(Object.fromEntries([...zAxes].map((axis) => [axis, 0])));
  };
  const takeControl = () => void run(() => api.POST("/api/v1/lease", { body: { force: true } }));
  const releaseControl = () => void run(() => api.DELETE("/api/v1/lease"));
  const reset = () => run(() => api.POST("/api/v1/machine/reset"));
  const connectMachine = () => void run(() => api.POST("/api/v1/machine/connect"));
  const setOutput = (id: string, on: boolean) =>
    void run(() =>
      api.PUT("/api/v1/machine/outputs/{output_id}", {
        params: { path: { output_id: id } },
        body: { on },
      }),
    );

  return (
    <div className="workspace">
      <aside className="sidebar">
        {(description?.cameras ?? []).map((camera) => (
          <CameraView
            key={camera.id}
            id={camera.id}
            title={camera.label}
            info={cameras?.find((c) => c.id === camera.id)}
          />
        ))}

        {/* Right under the cameras, so jogging and watching fit on one screen. */}
        {description && (
          <JogPanel
            description={description}
            machine={machine}
            canMove={canMove}
            onJog={jog}
            onSafeZ={safeZ}
          />
        )}

        <Panel title="Control">
          <LeaseStatus busy={pending} onTake={takeControl} onRelease={releaseControl} />
          {machine?.warnings.map((warning) => (
            <p key={warning} className="note note-warn">
              {warning}
            </p>
          ))}
          {machine && !machine.connected && (
            <div className="machine-offline">
              <p className="note note-danger">
                The machine is not connected
                {machine.connection_error ? `: ${machine.connection_error}` : "."}
              </p>
              <button
                type="button"
                className="button"
                disabled={pending || status !== "open"}
                onClick={connectMachine}
              >
                <Plug size={16} aria-hidden />
                Connect
              </button>
            </div>
          )}
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
          <p className={`note status-line${statusLine.danger ? " text-danger" : " text-muted"}`}>
            {statusLine.text}
          </p>
        </Panel>

        {description && (
          <PlacementPanel
            description={description}
            machine={machine}
            canMove={canMove && !!machine?.homed}
          />
        )}

        {feeders && feeders.fixed.length > 0 && (
          <FixedFeederPanel fixed={feeders.fixed} onChanged={setFeeders} />
        )}

        <CapturePanel />
        <RecordingsPanel />

        {description && (
          <BringUpPanel
            description={description}
            machine={machine}
            canMove={canMove}
            onHome={home}
            onJog={jog}
            onVisualHome={() => void track(() => api.POST("/api/v1/machine/visual-home"))}
            onCameraScale={() => void track(() => api.POST("/api/v1/machine/camera-scale"))}
            onScanFeeders={() => void track(() => api.POST("/api/v1/feeders/scan"))}
          />
        )}

        {description?.locations.some((loc) => loc.kind === "homing_fiducial") && (
          <CalibrationPanel
            operations={operations}
            canRun={canMove && !!machine?.homed}
            onVisualHome={() => void track(() => api.POST("/api/v1/machine/visual-home"))}
            onCameraScale={() => void track(() => api.POST("/api/v1/machine/camera-scale"))}
          />
        )}
      </aside>

      <div className="canvas machine-canvas">
        {description ? (
          <>
            <MachineView
              description={description}
              machine={machine}
              canMove={canMove && !!machine?.homed}
              onMoveTo={(p) => moveTo({ X: p.x, Y: p.y })}
              fixedFeeders={feeders?.fixed}
            />
            <HeadInset
              description={description}
              machine={machine}
              canActuate={canActuate}
              readOnly={controlledElsewhere}
              busy={pending}
              onSetOutput={setOutput}
            />
          </>
        ) : (
          <Placeholder
            icon={MapIcon}
            title="Machine view"
            description="Waiting for the machine description from the core."
          />
        )}
      </div>
    </div>
  );
}
