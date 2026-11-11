import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { CalibrationChecklist } from "./types";

/**
 * The calibration checklist. Fetched when the event socket connects, when an operation
 * ends (a homing or a measurement may have changed it), and when the machine is homed or
 * loses its homing.
 */
export function useCalibration(): CalibrationChecklist | null {
  const { status, clientId, operations, machine } = useCore();
  const [state, setState] = useState<CalibrationChecklist | null>(null);
  const ended = Object.values(operations).filter((op) => op.status !== "running").length;
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

  return state;
}
