const LENGTHS_MM = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100];

/** A round length for a camera's scale bar: the longest that takes at most a quarter of a
 * view this many millimeters wide. */
export function scaleLength(fieldOfView: number): number {
  const fits = LENGTHS_MM.filter((mm) => mm <= fieldOfView / 4);
  return fits.at(-1) ?? 0.1;
}
