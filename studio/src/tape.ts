/**
 * Carrier tape (EIA-481) for the Studio: geometry, the pin-1 quadrant, whether a part fits,
 * and reading values typed into the tapes table. The core holds the same rules
 * (picnicplace/library/models.py and tape.py).
 *
 * Tape frame: looking down on the cover tape, sprocket holes along the top, the tape
 * feeding left to right; +X along the feed, +Y toward the holes, angles counterclockwise.
 */
import type { Footprint, Tape } from "./api/types";

/** F: from the sprocket holes' center line to the pockets' center line, by width. */
export const POCKET_LINE: Record<number, number> = {
  8: 3.5,
  12: 5.5,
  16: 7.5,
  24: 11.5,
  32: 14.2,
  44: 20.2,
  56: 26.2,
};
export const TAPE_WIDTHS = Object.keys(POCKET_LINE).map(Number);
export const SPROCKET_EDGE = 1.75;
export const SPROCKET_PITCH = 4;
export const SPROCKET_D = 1.5;
/** P2: along the tape, from a sprocket hole to the nearest pocket center. */
export const POCKET_TO_SPROCKET = 2;

export type Quadrant = "Q1" | "Q2" | "Q3" | "Q4";

/** For a footprint with pin 1 at the top left at 0°: the rotation in tape for each of a
 * datasheet's pin-1 quadrants. */
export const QUADRANT_ROTATION: Record<Quadrant, number> = { Q1: 0, Q2: -90, Q3: 90, Q4: 180 };

/** Where each quadrant is in the pocket, in the tape frame (sprocket holes along the top). */
export const QUADRANT_PLACE: Record<Quadrant, string> = {
  Q1: "top left",
  Q2: "top right",
  Q3: "bottom left",
  Q4: "bottom right",
};

/** F for a width, or null for a width EIA-481 does not list. */
export function pocketLine(width: number | null): number | null {
  if (width === null) return null;
  return POCKET_LINE[Math.round(width)] ?? null;
}

/** Which quadrant of its pocket the footprint's pin-1 pad lands in, turned by `rotation`.
 * Null without exactly one marked pad, or with pin 1 on a center line (a diode). */
export function pin1Quadrant(footprint: Footprint, rotation: number): Quadrant | null {
  const marked = footprint.pads.filter((p) => p.pin1);
  if (marked.length !== 1) return null;
  const a = (rotation * Math.PI) / 180;
  const p = marked[0]!;
  const x = p.x * Math.cos(a) - p.y * Math.sin(a);
  const y = p.x * Math.sin(a) + p.y * Math.cos(a);
  if (Math.abs(x) < 1e-6 || Math.abs(y) < 1e-6) return null;
  if (y > 0) return x < 0 ? "Q1" : "Q2";
  return x < 0 ? "Q3" : "Q4";
}

/** Half the size of a footprint (pads and body), from its origin, at 0°. */
export function halfExtent(footprint: Footprint): { x: number; y: number } {
  let x = footprint.body ? footprint.body.x / 2 : 0;
  let y = footprint.body ? footprint.body.y / 2 : 0;
  for (const p of footprint.pads) {
    const a = (p.rotation * Math.PI) / 180;
    const c = Math.abs(Math.cos(a));
    const s = Math.abs(Math.sin(a));
    x = Math.max(x, Math.abs(p.x) + (p.width * c + p.height * s) / 2);
    y = Math.max(y, Math.abs(p.y) + (p.width * s + p.height * c) / 2);
  }
  return { x, y };
}

/** The size of a box (width along X, height along Y) once turned by `degrees`. */
export function turnedSize(size: { x: number; y: number }, degrees: number) {
  const a = (degrees * Math.PI) / 180;
  const c = Math.abs(Math.cos(a));
  const s = Math.abs(Math.sin(a));
  return { x: size.x * c + size.y * s, y: size.x * s + size.y * c };
}

export interface Fit {
  fits: boolean;
  /** The part's size in the tape: along the feed (x) and across it (y), mm. */
  part: { x: number; y: number };
  /** About how much room a pocket has, mm. */
  room: { x: number; y: number };
}

/**
 * Whether a part, turned by its rotation in tape, fits the room a tape leaves it: about
 * P1 - 0.5 mm along the tape, and across it twice the distance from the pocket's center
 * to the bottom edge (W - 1.75 - F), less 1 mm. An approximation of EIA-481's cavities.
 */
export function fitCheck(footprint: Footprint, tape: Tape): Fit | null {
  const line = pocketLine(tape.width);
  if (tape.width === null || tape.pitch === null || line === null) return null;
  const half = halfExtent(footprint);
  const part = turnedSize({ x: 2 * half.x, y: 2 * half.y }, tape.rotation ?? 0);
  const room = {
    x: tape.pitch - 0.5,
    y: 2 * (tape.width - SPROCKET_EDGE - line) - 1,
  };
  return { fits: part.x <= room.x + 1e-6 && part.y <= room.y + 1e-6, part, room };
}

/** A tape whose ID and name are made from its values, as the core makes them. */
export function standardTape(
  kind: Tape["kind"],
  width: number | null,
  pitch: number | null,
  rotation: number | null,
): Tape {
  const num = (v: number | null) => (v === null ? "na" : String(v));
  const kindWord = kind === "unknown" ? "" : ` ${kind}`;
  return {
    id: `${num(width)}mm-${kind}-p${num(pitch)}-r${num(rotation)}`,
    name:
      `${width === null ? "?" : width} mm${kindWord}` +
      (pitch === null ? "" : `, ${pitch} mm pitch`) +
      (rotation === null ? "" : `, ${rotation}°`),
    kind,
    width,
    pitch,
    rotation,
    basis: "unknown",
    source: "",
    notes: "",
  };
}

export type Parsed = { ok: true; value: number | null } | { ok: false; error: string };

const MM_PER: Record<string, number> = { mm: 1, cm: 10, in: 25.4, '"': 25.4, mil: 0.0254 };

/**
 * A length typed into the tapes table: a number, with or without a unit (mm, cm, in, ",
 * mil); without one it is millimeters. Empty clears the value (not known), and so does
 * zero or less. Anything else is refused, and the old value stays.
 */
export function parseLength(text: string): Parsed {
  const t = text.trim().toLowerCase();
  if (!t) return { ok: true, value: null };
  const m = /^([-+]?\d*\.?\d+)\s*(mm|cm|in|"|mil)?$/.exec(t);
  if (!m) return { ok: false, error: `"${text}" is not a length` };
  const mm = Number(m[1]) * MM_PER[m[2] ?? "mm"]!;
  return { ok: true, value: mm > 0 ? Math.round(mm * 1000) / 1000 : null };
}

/** A rotation typed into the tapes table, in degrees (a ° is allowed). Empty clears it. */
export function parseRotation(text: string): Parsed {
  const t = text.trim().replace(/°$/, "").trim();
  if (!t) return { ok: true, value: null };
  if (!/^[-+]?\d*\.?\d+$/.test(t)) return { ok: false, error: `"${text}" is not an angle` };
  return { ok: true, value: Number(t) };
}

/** A rotation as the tapes table shows it: one decimal place, or empty if not known. */
export function formatRotation(value: number | null): string {
  return value === null ? "" : `${value.toFixed(1)}°`;
}
