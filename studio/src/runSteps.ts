import type { RunItem, RunState } from "./api/types";

export type StepMark = "todo" | "done" | "failed" | "skipped";

/** One row of a run's step list. */
export interface RunStepRow {
  key: string;
  label: string;
  detail: string;
  mark: StepMark;
  /** The step under way, or where a paused run stopped. */
  current: boolean;
  /** A part picked and waiting on a nozzle. */
  held: boolean;
  /** The run paused on a problem at this step. */
  problem: boolean;
  item?: RunItem;
}

const ACTION: Record<NonNullable<RunState["current"]>["action"], string> = {
  scanning: "scanning",
  finding_fiducials: "finding fiducials",
  finding_feeder: "finding its feeder's fiducial",
  picking: "picking",
  aligning: "aligning",
  placing: "placing",
};

/** "slot 3" for a feeder slot, the ID for a strip or tray. */
export function sourceName(slotId: string | null): string {
  if (!slotId) return "no feeder";
  return /^\d+$/.test(slotId) ? `slot ${slotId}` : slotId;
}

/**
 * A run's steps in order: scanning the feeders (on a machine with a feeder bus), finding
 * the board, each placement, and the end. With no run yet, every step is still to do.
 */
export function runSteps(
  items: RunItem[],
  run: RunState | null,
  fiducials: number,
  hasBus: boolean,
): RunStepRow[] {
  const at = run?.current ?? null;
  const stopped = run?.status === "paused" && !!run.error;
  const nozzleOf = new Map(Object.entries(run?.held ?? {}).map(([n, i]) => [i, n]));
  const rows: RunStepRow[] = [];
  const row = (r: Omit<RunStepRow, "problem">): RunStepRow => ({
    ...r,
    problem: r.current && stopped,
  });

  if (hasBus) {
    const current = at?.stage === "scan";
    rows.push(
      row({
        key: "scan",
        label: "Scan feeders",
        detail: current ? ACTION.scanning : "",
        mark: run && (run.fix || at?.stage === "board") ? "done" : "todo",
        current,
        held: false,
      }),
    );
  }

  const fix = run?.fix;
  const boardCurrent = at?.stage === "board";
  let boardDetail = fiducials
    ? `${fiducials} fiducial${fiducials === 1 ? "" : "s"}`
    : "no fiducials: placed where the job says";
  if (boardCurrent) boardDetail = ACTION.finding_fiducials;
  else if (fix && fix.fiducials.length > 0) {
    const off = Math.hypot(fix.dx, fix.dy).toFixed(2);
    const turn = (fix.angle >= 0 ? "+" : "") + fix.angle.toFixed(2);
    boardDetail = `${off} mm off, turned ${turn}°`;
  }
  rows.push(
    row({
      key: "board",
      label: "Find the board",
      detail: boardDetail,
      mark: fix ? "done" : "todo",
      current: boardCurrent,
      held: false,
    }),
  );

  for (const item of items) {
    const current = at?.stage === "item" && at.index === item.index;
    const nozzle = nozzleOf.get(item.index);
    let detail: string;
    if (current) {
      const on = at.action === "placing" ? " with" : " on";
      detail = ACTION[at.action] + (at.nozzle ? `${on} ${at.nozzle}` : "");
    } else if (nozzle) detail = `on ${nozzle}`;
    else if (item.status === "placed") {
      detail = `placed${item.nozzle ? ` by ${item.nozzle}` : ""}`;
    }
    else if (item.status === "pending") {
      detail = `${item.part_id ?? "no part number"}, ${sourceName(item.slot_id)}`;
    } else detail = item.note || item.status;
    const mark: StepMark =
      item.status === "placed"
        ? "done"
        : item.status === "failed" || item.status === "blocked"
          ? "failed"
          : item.status === "skipped"
            ? "skipped"
            : "todo";
    rows.push(
      row({
        key: `item-${item.index}`,
        label: item.copy_id ? `${item.ref} ·${item.copy_id}` : item.ref,
        detail,
        mark,
        current,
        held: !current && !!nozzle,
        item,
      }),
    );
  }

  rows.push(
    row({
      key: "end",
      label: run?.status === "stopped" ? "Stopped" : "Finished",
      detail: run?.status === "finished" || run?.status === "stopped" ? (run.message ?? "") : "",
      mark: run?.status === "finished" ? "done" : run?.status === "stopped" ? "skipped" : "todo",
      current: false,
      held: false,
    }),
  );
  return rows;
}

/** The row to keep in view: the current step, else the next one to do, else the end. */
export function focusIndex(rows: RunStepRow[]): number {
  const current = rows.findIndex((r) => r.current);
  if (current >= 0) return current;
  const next = rows.findIndex((r) => r.mark === "todo");
  return next >= 0 ? next : rows.length - 1;
}
