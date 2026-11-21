import { describe, expect, it } from "vitest";
import type { FeedersState } from "./api/types";
import { lowReels } from "./reels";

function slot(id: string, uuid: string, left: number | null, low: number | null) {
  return {
    slot: { id, label: `Slot ${id}` },
    feeder: { uuid, assignment: { part_id: `PART-${id}`, reel_left: left, reel_low: low } },
  };
}

function state(...slots: ReturnType<typeof slot>[]): FeedersState {
  return { slots } as unknown as FeedersState;
}

describe("low reels", () => {
  it("are those at or under the level they warn at, and only those with one set", () => {
    const low = lowReels(
      state(
        slot("1", "A", 100, 50),
        slot("2", "B", 50, 50),
        slot("3", "C", 3, null),
        slot("4", "D", null, 10),
        slot("5", "E", 0, 20),
      ),
    );
    expect(low.map((r) => [r.label, r.left, r.key])).toEqual([
      ["Slot 2", 50, "B:low"],
      ["Slot 5", 0, "E:empty"],
    ]);
    expect(low[0]?.partId).toBe("PART-2");
  });
});
