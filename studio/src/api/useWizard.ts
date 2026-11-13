import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import { publishDescription, useDescription } from "./useDescription";
import type { WizardState } from "./types";

/** Operations whose results take the place of the profile's values. */
const MEASURING = new Set(["camera_scale", "bottom_camera", "nozzle_offsets"]);

/** Asks the Machine workspace to open the setup wizard, at a page if one is given. */
export const OPEN_WIZARD = "picnicplace:open-wizard";

export function openWizard(page?: string | null) {
  window.dispatchEvent(new CustomEvent(OPEN_WIZARD, { detail: page ?? null }));
}

/** Something the wizard's pages depend on changed (the bring-up checklist): fetch it again. */
const WIZARD_CHANGED = "picnicplace:wizard-changed";

export function wizardChanged() {
  window.dispatchEvent(new Event(WIZARD_CHANGED));
}

/**
 * The setup wizard: every page with where it stands, and the machine's stage. Fetched when
 * the event socket connects, when an operation ends (a measurement may have finished a
 * page), when the machine is homed or loses its homing, when the tips fitted or the
 * machine's locations change, and when `refresh` changes.
 */
export function useWizard(
  refresh = 0,
): [WizardState | null, (state: WizardState) => void] {
  const { status, clientId, operations, machine } = useCore();
  const description = useDescription();
  const [state, setState] = useState<WizardState | null>(null);
  const ops = Object.values(operations);
  const ended = ops.filter((op) => op.status !== "running").length;
  const measured = ops.filter((op) => MEASURING.has(op.kind) && op.status === "succeeded").length;
  const homed = !!machine?.homed;
  const fitted = description?.nozzles.map((n) => n.tip ?? "").join(",") ?? "";
  const places =
    description?.locations.map((l) => `${l.kind}:${l.position.x},${l.position.y}`).join(";") ??
    "";
  const [changed, setChanged] = useState(0);

  useEffect(() => {
    const bump = () => setChanged((n) => n + 1);
    window.addEventListener(WIZARD_CHANGED, bump);
    return () => window.removeEventListener(WIZARD_CHANGED, bump);
  }, []);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/wizard").then(({ data }) => {
      if (!cancelled && data) setState(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, clientId, ended, homed, fitted, places, changed, refresh]);

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

  return [state, setState];
}
