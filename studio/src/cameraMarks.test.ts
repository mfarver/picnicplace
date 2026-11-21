import { describe, expect, it } from "vitest";
import { placeMark, type CameraMark } from "./cameraMarks";

const mark: CameraMark = {
  kind: "part",
  x: 101,
  y: 202,
  width: 1.6,
  height: 0.8,
  angle: 90,
  title: "",
};

describe("placeMark", () => {
  it("puts a mark right of and above center when it is at +X and +Y", () => {
    const placed = placeMark(mark, { x: 100, y: 200 }, { x_points: "right", y_points: "back" });
    expect(placed.right).toBeCloseTo(1);
    expect(placed.down).toBeCloseTo(-2);
    // Machine +Y is up the picture: a mark turned 90 degrees points up, -90 in SVG.
    expect(placed.rotate).toBeCloseTo(-90);
  });

  it("follows flipped axes", () => {
    const placed = placeMark(mark, { x: 100, y: 200 }, { x_points: "left", y_points: "front" });
    expect(placed.right).toBeCloseTo(-1);
    expect(placed.down).toBeCloseTo(2);
    expect(placed.rotate).toBeCloseTo(90);
  });
});
