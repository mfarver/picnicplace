import { describe, expect, it } from "vitest";
import { formatAxis } from "./format";

describe("formatAxis", () => {
  it("formats linear axes in millimeters", () => {
    expect(formatAxis("X", 12.3456)).toBe("12.346");
  });

  it("formats rotary axes in degrees", () => {
    expect(formatAxis("A", -90)).toBe("-90.00°");
    // Whole turns are left out: a nozzle on a rotating coupling never winds back.
    expect(formatAxis("A", -2077.91)).toBe("82.09°");
    expect(formatAxis("B", -2393.65)).toBe("126.35°");
    expect(formatAxis("A", 540)).toBe("180.00°");
  });

  it("handles unknown positions", () => {
    expect(formatAxis("Y", undefined)).toBe("—");
  });
});
