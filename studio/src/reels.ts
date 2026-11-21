import type { FeedersState } from "./api/types";

/** A feeder whose reel is at or under the level it warns at. */
export interface LowReel {
  /** Which feeder, and whether low or empty: hiding one leaves the other to come. */
  key: string;
  label: string;
  partId: string;
  left: number;
  low: number;
}

/** The feeders whose counted reels are at or under their warning level, in slot order. */
export function lowReels(state: FeedersState): LowReel[] {
  const out: LowReel[] = [];
  for (const s of state.slots) {
    const a = s.feeder?.assignment;
    if (!s.feeder || !a || a.reel_left == null || a.reel_low == null) continue;
    if (a.reel_left > a.reel_low) continue;
    out.push({
      key: `${s.feeder.uuid}:${a.reel_left === 0 ? "empty" : "low"}`,
      label: s.slot.label,
      partId: a.part_id ?? "",
      left: a.reel_left,
      low: a.reel_low,
    });
  }
  return out;
}
