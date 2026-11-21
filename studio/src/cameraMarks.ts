/** Marks drawn on a top camera's picture: what vision found, in machine coordinates, placed
 * where it is in the picture as the head moves. */

import type { Footprint, SlotState } from "./api/types";

export type MarkKind = "part" | "empty" | "unknown" | "loose";

export interface CameraMark {
  kind: MarkKind;
  /** Center, machine X and Y in mm. */
  x: number;
  y: number;
  /** Along the mark's own X and Y, mm. */
  width: number;
  height: number;
  /** The mark's X axis, machine degrees counterclockwise. */
  angle: number;
  title: string;
}

/** The axes' directions in the picture, as the machine description gives them. */
export interface Orientation {
  x_points: "right" | "left";
  y_points: "back" | "front";
}

/** A mark in the picture: its center in mm right and down from the picture's center, and
 * its angle in degrees clockwise (SVG's). */
export interface PlacedMark {
  right: number;
  down: number;
  rotate: number;
}

export function placeMark(
  mark: CameraMark,
  camera: { x: number; y: number },
  o: Orientation,
): PlacedMark {
  const sx = o.x_points === "right" ? 1 : -1;
  const sy = o.y_points === "back" ? 1 : -1;
  const a = (mark.angle * Math.PI) / 180;
  // The mark's X axis in the picture: +X right flips with sx, +Y up (down negative).
  const rotate = (Math.atan2(-sy * Math.sin(a), sx * Math.cos(a)) * 180) / Math.PI;
  return { right: sx * (mark.x - camera.x), down: -sy * (mark.y - camera.y), rotate };
}

/** A footprint drawn on the picture: a part as it should lie, to hold up against the real
 * one (or the board's silkscreen). */
export interface CameraFootprint {
  footprint: Footprint;
  /** The footprint's origin, machine X and Y in mm. */
  x: number;
  y: number;
  /** The machine angle of the footprint's 0 degree direction (its +X), counterclockwise. */
  angle: number;
  title: string;
}

/**
 * The SVG matrix (a b c d e f) that draws a footprint, given in footprint millimeters with
 * Y negated as FootprintShape draws it, where it lies in the picture: turned to its machine
 * angle, and mirrored when the picture is (one axis flipped), so pin 1 lands on the right
 * corner either way. Translation is in mm right and down from the picture's center.
 */
export function footprintMatrix(
  f: { x: number; y: number; angle: number },
  camera: { x: number; y: number },
  o: Orientation,
): [number, number, number, number, number, number] {
  const sx = o.x_points === "right" ? 1 : -1;
  const sy = o.y_points === "back" ? 1 : -1;
  const a = (f.angle * Math.PI) / 180;
  const c = Math.cos(a);
  const s = Math.sin(a);
  // Picture = flip(sx, -sy) * turn(angle) * flip(1, -1) * drawn.
  return [sx * c, -sy * s, sx * s, sy * c, sx * (f.x - camera.x), -sy * (f.y - camera.y)];
}

/** A loose part's mark: about an 0603's size, not turned. */
const LOOSE_MM = 1.6;
/** Without an outline from the library, the pocket's mark is this big, not turned. */
const POCKET_MM = 1.2;

/** The marks for a slot's last pocket check: the pocket taken, and parts seen loose. */
export function pocketMarks(slot: SlotState): CameraMark[] {
  const p = slot.pocket;
  if (!p) return [];
  const kind: MarkKind = p.part === true ? "part" : p.part === false ? "empty" : "unknown";
  const said = { part: "holds a part", empty: "is empty", unknown: "was not judged" }[kind];
  const width = p.outline?.x ?? POCKET_MM;
  const height = p.outline?.y ?? POCKET_MM;
  const marks: CameraMark[] = [
    {
      kind,
      x: p.at.x,
      y: p.at.y,
      width,
      height,
      angle: p.angle ?? 0,
      title: `${slot.slot.label}: the pick pocket ${said}`,
    },
  ];
  for (const q of p.loose ?? []) {
    marks.push({
      kind: "loose",
      x: q.x,
      y: q.y,
      width: LOOSE_MM,
      height: LOOSE_MM,
      angle: 0,
      title: "A part lying loose on the tape",
    });
  }
  return marks;
}
