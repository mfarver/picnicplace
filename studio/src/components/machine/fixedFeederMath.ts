import type { FixedFeederState, Point } from "../../api/types";

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
