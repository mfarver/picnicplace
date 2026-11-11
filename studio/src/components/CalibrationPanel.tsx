import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  CircleDot,
  Crosshair,
  Focus,
  Home,
  Minus,
  RefreshCw,
  Ruler,
  SlidersHorizontal,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCalibration } from "../api/useCalibration";
import type { CalibrationLine, MachineProfile, Operation } from "../api/types";
import { MachineLocations } from "./MachineLocations";
import { Panel } from "./Panel";

const VISION_KINDS = new Set([
  "home",
  "visual_home",
  "camera_scale",
  "bottom_camera",
  "nozzle_offsets",
]);

const STATUS: Record<CalibrationLine["status"], { label: string; icon: ReactNode }> = {
  done: { label: "Done", icon: <CheckCircle2 size={14} aria-hidden /> },
  needed: { label: "To do", icon: <Circle size={14} aria-hidden /> },
  stale: { label: "Out of date", icon: <AlertTriangle size={14} aria-hidden /> },
  assumed: { label: "From the profile", icon: <SlidersHorizontal size={14} aria-hidden /> },
  every_run: { label: "Each run", icon: <RefreshCw size={14} aria-hidden /> },
  not_needed: { label: "Not needed", icon: <Minus size={14} aria-hidden /> },
};

interface Props {
  description: MachineProfile;
  operations: Record<string, Operation>;
  /** This client may move the machine. */
  canMove: boolean;
  /** Homed, and this client may move the machine. */
  canRun: boolean;
  /** Homed, and this client may change the machine's locations. */
  canSet: boolean;
  onHome: () => void;
  onVisualHome: () => void;
  onCameraScale: () => void;
  onBottomCamera: () => void;
  onNozzleOffsets: () => void;
}

/**
 * The calibration checklist (spec §7.7): every calibration the machine needs, in order,
 * whether each is done, its value and when, or why it is out of date, with a button for
 * those the machine can do. Then the machine's own locations.
 */
export function CalibrationPanel({
  description,
  operations,
  canMove,
  canRun,
  canSet,
  onHome,
  onVisualHome,
  onCameraScale,
  onBottomCamera,
  onNozzleOffsets,
}: Props) {
  const checklist = useCalibration();
  const latest = Object.values(operations)
    .filter((op) => VISION_KINDS.has(op.kind) && op.status === "failed")
    .at(-1);
  const lines = checklist?.lines ?? [];
  const todo = lines.filter((l) => l.status === "needed").length;
  const stale = lines.filter((l) => l.status === "stale").length;

  const action = (line: CalibrationLine) => {
    switch (line.action) {
      case "home":
        return (
          <button type="button" className="button" disabled={!canMove} onClick={onHome}>
            <Home size={16} aria-hidden />
            Home
          </button>
        );
      case "visual_home":
        return (
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
        );
      case "camera_scale":
        return (
          <button
            type="button"
            className="button"
            disabled={!canRun}
            onClick={onCameraScale}
            title="Measure the top camera's millimeters per pixel over the homing fiducial"
          >
            <Ruler size={16} aria-hidden />
            {line.status === "done" ? "Measure again" : "Measure"}
          </button>
        );
      case "bottom_camera":
        return (
          <button
            type="button"
            className="button"
            disabled={!canRun}
            onClick={onBottomCamera}
            title="With an empty nozzle tip: find the camera's place, focus, and scale"
          >
            <Focus size={16} aria-hidden />
            {line.status === "done" ? "Calibrate again" : "Calibrate"}
          </button>
        );
      case "nozzle_offsets":
        return (
          <button
            type="button"
            className="button"
            disabled={!canRun}
            onClick={onNozzleOffsets}
            title={
              "With the top camera centered on a paper disc on the datum board: each nozzle " +
              "sniffs for the board's height, then turns the disc to find where it is"
            }
          >
            <CircleDot size={16} aria-hidden />
            {line.status === "done" ? "Calibrate again" : "Calibrate"}
          </button>
        );
      case "calibrate_feeders":
        return line.status === "done" ? null : (
          <span className="text-muted">In the Feeders workspace: Calibrate rails.</span>
        );
      default:
        return null;
    }
  };

  return (
    <Panel title="Calibration" collapseId="calibration">
      {checklist && (
        <p className={`note${todo || stale ? " note-warn" : " text-muted"}`}>
          {todo || stale
            ? [todo && `${todo} to do`, stale && `${stale} out of date`].filter(Boolean).join(", ")
            : "Everything the machine can calibrate is done."}
        </p>
      )}
      <ol className="calibration-list">
        {lines.map((line) => (
          <li key={line.id} className={`calibration-line calibration-${line.status}`}>
            <div className="calibration-title">
              <span className="calibration-number">{line.number}</span>
              <span className="calibration-name">{line.title}</span>
              <span className="calibration-chip">
                {STATUS[line.status].icon}
                {STATUS[line.status].label}
              </span>
            </div>
            <div className="calibration-body">
              <p>
                {line.detail}
                {line.measured_at && (
                  <span className="text-faint">
                    {" "}
                    ({new Date(line.measured_at).toLocaleString()})
                  </span>
                )}
              </p>
              {line.stale && <p className="text-warn">Out of date: {line.stale}.</p>}
              {line.warning && <p className="text-warn">{line.warning}.</p>}
              {action(line)}
            </div>
          </li>
        ))}
      </ol>
      {latest && <p className="note note-danger">{latest.error}</p>}
      <MachineLocations description={description} canSet={canSet} />
    </Panel>
  );
}
