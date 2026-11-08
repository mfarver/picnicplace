import type { Footprint } from "../../api/types";

/** The footprint's extent, for fitting a view: half-width and half-height, mm. */
export function footprintExtent(footprint: Footprint): { x: number; y: number } {
  let x = (footprint.body?.x ?? 0) / 2;
  let y = (footprint.body?.y ?? 0) / 2;
  for (const pad of footprint.pads) {
    const reach = Math.hypot(pad.width, pad.height) / 2;
    x = Math.max(x, Math.abs(pad.x) + reach);
    y = Math.max(y, Math.abs(pad.y) + reach);
  }
  return { x: Math.max(x, 0.5), y: Math.max(y, 0.5) };
}
