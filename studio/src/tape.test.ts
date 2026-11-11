import { describe, expect, it } from "vitest";
import type { Footprint, Pad } from "./api/types";
import {
  QUADRANT_ROTATION,
  fitCheck,
  parseLength,
  parseRotation,
  formatRotation,
  pin1Quadrant,
  standardTape,
} from "./tape";

function pad(name: string, x: number, y: number, w: number, h: number, pin1 = false): Pad {
  return { name, x, y, width: w, height: h, rotation: 0, roundness: 0, pin1 };
}

function footprint(pads: Pad[], body: Footprint["body"] = null): Footprint {
  return { id: "F", description: "", body, body_basis: "unknown", pads, source: "" };
}

const R0603 = footprint([pad("1", -0.8, 0, 0.8, 0.95, true), pad("2", 0.8, 0, 0.8, 0.95)], {
  x: 1.6,
  y: 0.8,
});

/** About 9 x 9 mm with leads, pin 1 at the top left. */
const TQFP32 = footprint(
  [pad("1", -4.2, 2.8, 1.5, 0.45, true), pad("9", -2.8, -4.2, 0.45, 1.5)],
  { x: 7, y: 7 },
);

describe("parseLength", () => {
  it("reads millimeters, other units, and clears", () => {
    expect(parseLength("4")).toEqual({ ok: true, value: 4 });
    expect(parseLength(" 4 mm ")).toEqual({ ok: true, value: 4 });
    expect(parseLength('0.157"')).toEqual({ ok: true, value: 3.988 });
    expect(parseLength("0.315in")).toEqual({ ok: true, value: 8.001 });
    expect(parseLength("")).toEqual({ ok: true, value: null });
    expect(parseLength("0")).toEqual({ ok: true, value: null });
    expect(parseLength("-2")).toEqual({ ok: true, value: null });
    expect(parseLength("four").ok).toBe(false);
  });
});

describe("parseRotation", () => {
  it("reads degrees and clears", () => {
    expect(parseRotation("-90")).toEqual({ ok: true, value: -90 });
    expect(parseRotation("45.5°")).toEqual({ ok: true, value: 45.5 });
    expect(parseRotation("")).toEqual({ ok: true, value: null });
    expect(parseRotation("left").ok).toBe(false);
    expect(formatRotation(90)).toBe("90.0°");
    expect(formatRotation(null)).toBe("");
  });
});

describe("pin1Quadrant", () => {
  it("matches the datasheet table for pin 1 at the top left", () => {
    for (const [q, rotation] of Object.entries(QUADRANT_ROTATION)) {
      expect(pin1Quadrant(TQFP32, rotation)).toBe(q);
    }
  });

  it("does not say for pin 1 on a center line or no mark", () => {
    expect(pin1Quadrant(R0603, 0)).toBeNull();
    expect(pin1Quadrant(footprint([pad("1", -1, 1, 1, 1)]), 0)).toBeNull();
  });
});

describe("fitCheck", () => {
  it("fits a 0603 in 8 mm tape at 4 mm pitch", () => {
    const fit = fitCheck(R0603, standardTape("paper", 8, 4, 0))!;
    expect(fit.fits).toBe(true);
    expect(fit.room).toEqual({ x: 3.5, y: 4.5 });
  });

  it("does not fit a TQFP-32 in 8 mm tape, and does in 16 mm at 12 mm pitch", () => {
    expect(fitCheck(TQFP32, standardTape("embossed", 8, 4, 0))!.fits).toBe(false);
    expect(fitCheck(TQFP32, standardTape("embossed", 16, 12, 0))!.fits).toBe(true);
  });

  it("needs a width and pitch", () => {
    expect(fitCheck(R0603, standardTape("paper", null, 4, 0))).toBeNull();
  });
});

describe("standardTape", () => {
  it("names tapes as the core does", () => {
    const t = standardTape("paper", 8, 4, 90);
    expect(t.id).toBe("8mm-paper-p4-r90");
    expect(t.name).toBe("8 mm paper, 4 mm pitch, 90°");
    expect(standardTape("unknown", null, 4, null).id).toBe("namm-unknown-p4-rna");
  });
});
