import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { CalibrationChecklist } from "./types";
import { publishDescription } from "./useDescription";

/** Operations whose results take the place of the profile's values. */
const MEASURING = new Set(["camera_scale", "bottom_camera", "nozzle_offsets"]);

/**
 * The calibration checklist. Fetched when the event socket connects, when an operation
 * ends (a homing or a measurement may have changed it), and when the machine is homed or
 * loses its homing.
 */
export function useCalibration(): CalibrationChecklist | null {
  const { status, clientId, operations, machine } = useCore();
  const [state, setState] = useState<CalibrationChecklist | null>(null);
  const ops = Object.values(operations);
  const ended = ops.filter((op) => op.status !== "running").length;
  const measured = ops.filter((op) => MEASURING.has(op.kind) && op.status === "succeeded").length;
  const homed = !!machine?.homed;

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/calibration").then(({ data }) => {
      if (!cancelled && data) setState(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, clientId, ended, homed]);

  // A measurement changes the machine description (a camera's scale or place): show it.
  useEffect(() => {
    if (status !== "open" || measured === 0) return;
    let cancelled = false;
    void api.GET("/api/v1/machine/description").then(({ data }) => {
      if (!cancelled && data) publishDescription(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, measured]);

  return state;
}
