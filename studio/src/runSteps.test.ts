import { describe, expect, it } from "vitest";
import type { RunItem, RunState } from "./api/types";
import { focusIndex, padsMoved, runSteps } from "./runSteps";

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
    // Its pads not seen: where it landed from where it was sent.
    const unseen = { on_pads: null, pads_dx: null, pads_dy: null };
    const good = { dx: 0.03, dy: -0.04, turn: 1.2, score: 0.9, ok: true, picture: true, ...unseen };
    const bad = { dx: -0.12, dy: 0.41, turn: 9, score: 0.8, ok: false, picture: true, ...unseen };
    const square = { ...good, turn: -0.04 };
    const items = [
      item(0, "R1", "placed", { nozzle: "N1", check: bad }),
      item(1, "R2", "placed", { nozzle: "N2", check: good }),
      item(2, "R3", "placed", { nozzle: "N2", check: square }),
    ];
    const rows = runSteps(items, run(items, {}), 0, false);
    expect(rows[1]).toMatchObject({
      detail: "placed by N1, landed 0.43 mm off, +9.0°: look at it",
    });
    expect(rows[2]).toMatchObject({ detail: "placed by N2, 0.05 mm off, +1.2°" });
    expect(rows[3]).toMatchObject({ detail: "placed by N2, 0.05 mm off, +0.0°" });
  });

  it("says how much of a checked part is on its pads, and when the board moved", () => {
    const pads = { pads_dx: 0.02, pads_dy: -0.03 };
    const good = { dx: 0.03, dy: -0.04, turn: 0.5, score: 0.9, ok: true, picture: true };
    const onPads = { ...good, ...pads, on_pads: 0.98 };
    const moved = { ...good, on_pads: 0.97, pads_dx: 0.1, pads_dy: 0.2 };
    const items = [
      item(0, "R1", "placed", { nozzle: "N1", check: onPads }),
      item(1, "R2", "placed", {
        nozzle: "N2",
        check: { ...good, ...pads, on_pads: 0.23, turn: -3, ok: false },
      }),
      item(2, "R3", "placed", {
        nozzle: "N1",
        check: { ...good, on_pads: 0.72, pads_dx: -0.02, pads_dy: 0.32, ok: false },
      }),
      item(3, "R4", "placed", { nozzle: "N2", check: moved }),
    ];
    const rows = runSteps(items, run(items, {}), 0, false);
    expect(rows[1]).toMatchObject({ detail: "placed by N1, 98% on its pads, +0.5°" });
    expect(rows[2]).toMatchObject({
      detail: "placed by N2, only 23% on its pads, -3.0°: look at it",
    });
    expect(rows[3]).toMatchObject({
      detail:
        "placed by N1, only 72% on its pads, +0.5°: look at it; its pads were 0.32 mm off: " +
        "the board moved",
    });
    expect(rows[4]).toMatchObject({
      detail: "placed by N2, 97% on its pads, +0.5°; its pads were 0.22 mm off: the board moved",
    });
    expect(padsMoved(onPads)).toBeNull();
    expect(padsMoved(moved)).toBeCloseTo(0.224, 3);
  });

  it("says where a part landed while the run is paused on it", () => {
    const bad = {
      dx: 0.32,
      dy: 0.12,
      turn: -0.5,
      score: 0.8,
      ok: false,
      picture: true,
      on_pads: null,
      pads_dx: null,
      pads_dy: null,
    };
    const items = [item(0, "R2", "placed", { nozzle: "N2", check: bad })];
    const paused = run(items, {
      status: "paused",
      current: { stage: "item", action: "checking", index: 0, nozzle: "N2" },
    });
    expect(runSteps(items, paused, 0, false)[1]).toMatchObject({
      current: true,
      detail: "placed by N2, landed 0.34 mm off, -0.5°: look at it",
    });
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
