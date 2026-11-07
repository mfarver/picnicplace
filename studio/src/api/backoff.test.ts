import { describe, expect, it } from "vitest";
import { reconnectDelayMs } from "./backoff";

describe("reconnectDelayMs", () => {
  it("doubles from 500 ms", () => {
    expect([0, 1, 2, 3].map(reconnectDelayMs)).toEqual([500, 1000, 2000, 4000]);
  });

  it("caps at 5 s", () => {
    expect(reconnectDelayMs(10)).toBe(5000);
  });
});
