import { Crosshair, Ruler } from "lucide-react";
import type { Operation } from "../api/types";
import { Panel } from "./Panel";

const VISION_KINDS = new Set(["home", "visual_home", "camera_scale"]);

interface Props {
  operations: Record<string, Operation>;
  canRun: boolean;
  onVisualHome: () => void;
  onCameraScale: () => void;
}

/** Vision-based calibration steps, and the latest thing vision found. */
export function CalibrationPanel({ operations, canRun, onVisualHome, onCameraScale }: Props) {
  const latest = Object.values(operations)
    .filter((op) => VISION_KINDS.has(op.kind) && (op.message || op.status === "failed"))
    .at(-1);
  return (
    <Panel title="Calibration" collapseId="calibration">
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
    </Panel>
  );
}
