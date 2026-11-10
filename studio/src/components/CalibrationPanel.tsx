import { Crosshair, Ruler } from "lucide-react";
import type { MachineProfile, Operation } from "../api/types";
import { MachineLocations } from "./MachineLocations";
import { Panel } from "./Panel";

const VISION_KINDS = new Set(["home", "visual_home", "camera_scale"]);

interface Props {
  description: MachineProfile;
  operations: Record<string, Operation>;
  canRun: boolean;
  /** Homed, and this client may change the machine's locations. */
  canSet: boolean;
  onVisualHome: () => void;
  onCameraScale: () => void;
}

/** Vision-based calibration steps, and the latest thing vision found. */
export function CalibrationPanel({
  description,
  operations,
  canRun,
  canSet,
  onVisualHome,
  onCameraScale,
}: Props) {
  const fiducial = description.locations.some((loc) => loc.kind === "homing_fiducial");
  const latest = Object.values(operations)
    .filter((op) => VISION_KINDS.has(op.kind) && (op.message || op.status === "failed"))
    .at(-1);
  return (
    <Panel title="Calibration" collapseId="calibration">
      {fiducial && (
        <>
          <div className="button-row">
            <button
              type="button"
              className="button"
              disabled={!canRun}
              onClick={onVisualHome}
              title="Center the top camera on the homing fiducial and correct X and Y"
            >
              <Crosshair size={16} aria-hidden />
              Visual home
            </button>
            <button
              type="button"
              className="button"
              disabled={!canRun}
              onClick={onCameraScale}
              title="Measure the top camera's millimeters per pixel over the homing fiducial"
            >
              <Ruler size={16} aria-hidden />
              Camera scale
            </button>
          </div>
          {latest && (
            <p className={`note${latest.status === "failed" ? " note-danger" : " text-muted"}`}>
              {latest.status === "failed" ? latest.error : latest.message}
            </p>
          )}
        </>
      )}
      <MachineLocations description={description} canSet={canSet} />
    </Panel>
  );
}
