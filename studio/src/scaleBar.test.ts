import { describe, expect, it } from "vitest";
import { scaleLength } from "./scaleBar";

describe("scaleLength", () => {
  it("takes a round length of at most a quarter of the view", () => {
    expect(scaleLength(20.6)).toBe(5); // LumenPnP top camera
    expect(scaleLength(39.6)).toBe(5); // and its bottom camera
    expect(scaleLength(8)).toBe(2);
    expect(scaleLength(100)).toBe(20);
    expect(scaleLength(0.2)).toBe(0.1);
  });
});
