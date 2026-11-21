import type { FixedFeederState, Point } from "../../api/types";
import { POCKET_TO_SPROCKET, SPROCKET_EDGE, SPROCKET_PITCH, pocketLine } from "../../tape";

/** Where part ``index`` (from 0) lies: along each row, then row by row. */
export function partPosition(f: FixedFeederState["feeder"], index: number): Point {
  const column = index % f.columns;
  const row = Math.floor(index / f.columns);
  return {
    x: f.first.x + column * f.step.x + row * f.row_step.x,
    y: f.first.y + column * f.step.y + row * f.row_step.y,
  };
}

/** "7 of 12 left", or "empty". */
export function remainingText(s: FixedFeederState): string {
  const capacity = s.feeder.columns * s.feeder.rows;
  return s.remaining === 0 ? "empty" : `${s.remaining} of ${capacity} left`;
}

/** A strip's tape as it lies on the bed, in machine millimeters. */
export interface StripTape {
  /** The tape's outline: four corners. */
  outline: Point[];
  /** Sprocket hole centers. */
  holes: Point[];
}

/** How far a sprocket hole is kept in from the ends of the drawn tape, mm. */
const HOLE_MARGIN = 0.75;

/**
 * A one-row strip's tape, to scale: as wide as the holder's tape, running half a pitch
 * past the first and last parts, with its sprocket holes on the side EIA-481 puts them,
 * to the left looking along the feed (which runs opposite to the step). Null when the
 * strip's tape width is not known or not a standard one.
 */
export function stripTape(f: FixedFeederState["feeder"]): StripTape | null {
  const width = f.tape_width ?? null;
  const line = pocketLine(width);
  const pitch = Math.hypot(f.step.x, f.step.y);
  if (f.kind !== "strip" || f.rows !== 1 || width === null || line === null || !pitch) {
    return null;
  }
  // s: a unit step, from one part to the next; h: toward the sprocket holes, to the left
  // of the feed (-s).
  const s = { x: f.step.x / pitch, y: f.step.y / pitch };
  const h = { x: s.y, y: -s.x };
  const at = (along: number, across: number): Point => ({
    x: f.first.x + along * s.x + across * h.x,
    y: f.first.y + along * s.y + across * h.y,
  });
  const start = -pitch / 2;
  const end = (f.columns - 0.5) * pitch;
  const near = line + SPROCKET_EDGE;
  const far = near - width;
  // Holes lie P2 from every pocket: with a pitch that is a multiple of 4 mm, on one grid.
  const holes: Point[] = [];
  let d = POCKET_TO_SPROCKET - SPROCKET_PITCH * Math.ceil((POCKET_TO_SPROCKET - start) / 4);
  for (; d <= end - HOLE_MARGIN + 1e-9; d += SPROCKET_PITCH) {
    if (d >= start + HOLE_MARGIN - 1e-9) holes.push(at(d, line));
  }
  return {
    outline: [at(start, near), at(end, near), at(end, far), at(start, far)],
    holes,
  };
}
