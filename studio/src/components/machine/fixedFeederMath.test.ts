import { describe, expect, it } from "vitest";
import type { FixedFeederState } from "../../api/types";
import { stripTape } from "./fixedFeederMath";

function strip(over: Partial<FixedFeederState["feeder"]> = {}): FixedFeederState["feeder"] {
  return {
    label: "",
    kind: "strip",
    part_id: null,
    first: { x: 100, y: 200 },
    step: { x: 0, y: 12 },
    columns: 5,
    rows: 1,
    row_step: { x: 0, y: 0 },
    part_angle: 0,
    pick_height: null,
    picked: 0,
    tape_width: 16,
    enabled: true,
    ...over,
  };
}

describe("stripTape", () => {
  it("draws 16 mm tape feeding toward -Y with its holes on the +X side", () => {
    const tape = stripTape(strip())!;
    // Feeding toward -Y, the left is +X. F is 7.5 mm, the holes 1.75 mm in from the edge.
    const xs = tape.outline.map((p) => p.x);
    expect(Math.max(...xs)).toBeCloseTo(100 + 7.5 + 1.75);
    expect(Math.min(...xs)).toBeCloseTo(100 + 7.5 + 1.75 - 16);
    const ys = tape.outline.map((p) => p.y);
    expect(Math.min(...ys)).toBeCloseTo(200 - 6);
    expect(Math.max(...ys)).toBeCloseTo(200 + 4 * 12 + 6);
    expect(tape.holes.every((h) => Math.abs(h.x - 107.5) < 1e-9)).toBe(true);
    // Every hole 2 mm from a pocket, 4 mm apart, all on the tape.
    for (const h of tape.holes) expect(Math.abs((((h.y - 200) % 4) + 4) % 4)).toBeCloseTo(2);
    expect(tape.holes.map((h) => h.y)).toEqual([
      198, 202, 206, 210, 214, 218, 222, 226, 230, 234, 238, 242, 246, 250,
    ]);
  });

  it("puts the holes on the other side when the strip runs the other way", () => {
    const tape = stripTape(strip({ step: { x: 0, y: -12 } }))!;
    expect(tape.holes[0]!.x).toBeCloseTo(100 - 7.5);
  });

  it("draws nothing without a standard tape width, or for a tray", () => {
    expect(stripTape(strip({ tape_width: null }))).toBeNull();
    expect(stripTape(strip({ tape_width: 15 }))).toBeNull();
    expect(stripTape(strip({ kind: "tray" }))).toBeNull();
  });
});
