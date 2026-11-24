import { describe, expect, it } from "vitest";
import { placementsPerHour } from "./runSpeed";

const stats = (placed: number, working_s: number) => ({
  picks: placed,
  empty_picks: 0,
  alignments: placed,
  failed_alignments: 0,
  placed,
  checked: 0,
  unsure: 0,
  bad_placements: 0,
  working_s,
});

describe("placementsPerHour", () => {
  it("leaves the pauses out of the working rate, and in the overall rate", () => {
    // 180 placed in 30 minutes of work, over a run of an hour from start to end.
    const run = {
      stats: stats(180, 1800),
      working_since: null,
      started_at: "2026-11-23T10:00:00Z",
      finished_at: "2026-11-23T11:00:00Z",
    };
    expect(placementsPerHour(run)).toEqual({ working: 360, total: 180 });
  });

  it("counts the time worked so far, and the run so far, while it is running", () => {
    const now = Date.parse("2026-11-23T10:20:00Z");
    // 10 minutes counted, working again for the last 5: 60 placed in 15 minutes of 20.
    const run = {
      stats: stats(60, 600),
      working_since: "2026-11-23T10:15:00Z",
      started_at: "2026-11-23T10:00:00Z",
      finished_at: null,
    };
    expect(placementsPerHour(run, now)).toEqual({ working: 240, total: 180 });
  });

  it("gives no rate before any time has passed", () => {
    const run = {
      stats: stats(0, 0),
      working_since: null,
      started_at: "2026-11-23T10:00:00Z",
      finished_at: "2026-11-23T10:00:00Z",
    };
    expect(placementsPerHour(run)).toEqual({ working: null, total: null });
  });
});
