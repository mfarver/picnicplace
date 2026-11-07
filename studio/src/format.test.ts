import { describe, expect, it } from "vitest";
import { formatAxis } from "./format";

describe("formatAxis", () => {
  it("formats linear axes in millimeters", () => {
    expect(formatAxis("X", 12.3456)).toBe("12.346");
  });

  it("formats rotary axes in degrees", () => {
    expect(formatAxis("A", -90)).toBe("-90.00°");
  });

  it("handles unknown positions", () => {
    expect(formatAxis("Y", undefined)).toBe("—");
  });
});
