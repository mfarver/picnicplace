import { describe, expect, it } from "vitest";
import type { RunItem, RunState } from "./api/types";
import { focusIndex, runSteps } from "./runSteps";

function item(
  index: number,
  ref: string,
  status: RunItem["status"],
  extra: Partial<RunItem> = {},
) {
  return {
    index,
    copy_id: null,
    ref,
    part_id: "EX-R0603-10K",
    footprint_id: "R_0603",
    x: 0,
    y: 0,
    rotation: 0,
    slot_id: "3",
    nozzles: ["N1", "N2"],
    status,
    note: "",
    nozzle: null,
    discards: 0,
    ...extra,
  } as RunItem;
}

const fix = {
  dx: 0.6,
  dy: -0.8,
  angle: 0.5,
  about: { x: 0, y: 0 },
  residual: 0.01,
  fiducials: [{ ref: "FID1", copy_id: null, expected: { x: 0, y: 0 }, found: { x: 0, y: 0 } }],
};

function run(items: RunItem[], extra: Partial<RunState>): RunState {
  return {
    job_id: "demo",
    name: "Demo",
    status: "running",
    plan: { job_id: "demo", name: "Demo", items, parts: [], fiducials: 1, issues: [] },
    fix: null,
    held: {},
    current: null,
    located: [],
    ...extra,
  } as unknown as RunState;
}

describe("runSteps", () => {
  it("lists every step still to do once a job is planned", () => {
    const items = [item(0, "R1", "pending"), item(1, "R2", "blocked", { note: "no height" })];
    const rows = runSteps(items, null, 2, true);
    expect(rows.map((r) => r.label)).toEqual([
      "Scan feeders",
      "Find the board",
      "R1",
      "R2",
      "Finished",
    ]);
    expect(rows.map((r) => r.mark)).toEqual(["todo", "todo", "todo", "failed", "todo"]);
    expect(rows.some((r) => r.current)).toBe(false);
    expect(focusIndex(rows)).toBe(0); // nothing started: the first step to do
    expect(rows[2]?.detail).toBe("EX-R0603-10K, slot 3");
  });

  it("checks off what is done and highlights the step under way", () => {
    const items = [
      item(0, "R1", "placed", { nozzle: "N1" }),
      item(1, "R2", "pending"),
      item(2, "R3", "pending"),
    ];
    const state = run(items, {
      fix,
      held: { N1: 1 },
      current: { stage: "item", action: "picking", index: 2, nozzle: "N2" },
    });
    const rows = runSteps(items, state, 1, false);
    expect(rows[0]).toMatchObject({
      label: "Find the board",
      mark: "done",
      detail: "1.00 mm off, turned +0.50°",
    });
    expect(rows[1]).toMatchObject({ mark: "done", detail: "placed by N1" });
    expect(rows[2]).toMatchObject({ held: true, current: false, detail: "on N1" });
    expect(rows[3]).toMatchObject({ current: true, problem: false, detail: "picking on N2" });
    expect(focusIndex(rows)).toBe(3);
  });

  it("says where a checked part landed", () => {
    const good = { dx: 0.03, dy: -0.04, turn: 1.2, score: 0.9, ok: true, picture: true };
    const bad = { dx: -0.12, dy: 0.41, turn: 9, score: 0.8, ok: false, picture: true };
    const items = [
      item(0, "R1", "placed", { nozzle: "N1", check: bad }),
      item(1, "R2", "placed", { nozzle: "N2", check: good }),
    ];
    const rows = runSteps(items, run(items, {}), 0, false);
    expect(rows[1]).toMatchObject({ detail: "placed by N1, landed 0.43 mm off, +9°: look at it" });
    expect(rows[2]).toMatchObject({ detail: "placed by N2, 0.05 mm off, +1°" });
  });

  it("shows the board's top once it is found by touch", () => {
    const items = [item(0, "R1", "pending")];
    const measuring = run(items, {
      fix,
      current: { stage: "board", action: "measuring_board", index: null, nozzle: null },
    });
    expect(runSteps(items, measuring, 1, false)[0]).toMatchObject({
      mark: "todo",
      current: true,
      detail: "finding the board's top",
    });
    const found = run(items, { fix, board_z: -26.9 });
    expect(runSteps(items, found, 1, false)[0]).toMatchObject({
      mark: "done",
      detail: "1.00 mm off, turned +0.50°, top at Z -26.90",
    });
  });

  it("marks where a problem paused the run", () => {
    const items = [item(0, "R1", "pending")];
    const state = run(items, {
      status: "paused",
      error: "slot 3: could not reach the next part",
      current: { stage: "scan", action: "scanning", index: null, nozzle: null },
    });
    const rows = runSteps(items, state, 1, true);
    expect(rows[0]).toMatchObject({ label: "Scan feeders", current: true, problem: true });
  });
});
