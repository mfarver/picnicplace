import { describe, expect, it } from "vitest";
import { footprintMatrix, placeMark, type CameraMark } from "./cameraMarks";

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

/** Where a footprint point (footprint mm, +Y up) lands in the picture: mm right and down. */
function land(m: number[], u: number, v: number): [number, number] {
  const [a, b, c, d, e, f] = m as [number, number, number, number, number, number];
  // FootprintShape draws (u, -v); the matrix takes it into the picture.
  return [a * u + c * -v + e, b * u + d * -v + f];
}

describe("footprintMatrix", () => {
  const camera = { x: 100, y: 200 };
  const pin1 = [-1, 1] as const; // top left of the footprint

  it("draws an unturned footprint as it is, at its place", () => {
    const m = footprintMatrix({ x: 101, y: 200, angle: 0 }, camera, {
      x_points: "right",
      y_points: "back",
    });
    const [right, down] = land(m, ...pin1);
    expect(right).toBeCloseTo(0);
    expect(down).toBeCloseTo(-1);
  });

  it("turns it counterclockwise as the machine sees it", () => {
    // At 90 degrees the top left corner turns to the bottom left: (-1, -1).
    const m = footprintMatrix({ x: 100, y: 200, angle: 90 }, camera, {
      x_points: "right",
      y_points: "back",
    });
    const [right, down] = land(m, ...pin1);
    expect(right).toBeCloseTo(-1);
    expect(down).toBeCloseTo(1);
  });

  it("mirrors it with a picture flipped left to right", () => {
    const m = footprintMatrix({ x: 100, y: 200, angle: 0 }, camera, {
      x_points: "left",
      y_points: "back",
    });
    const [right, down] = land(m, ...pin1);
    expect(right).toBeCloseTo(1);
    expect(down).toBeCloseTo(-1);
  });
});
