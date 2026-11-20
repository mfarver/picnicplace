import { describe, expect, it } from "vitest";
import type { Job } from "./api/types";
import { fiducialChoices } from "./fiducials";

function mark(ref: string, side: "top" | "bottom", kind: "fiducial" | "part" = "fiducial") {
  return { ref, kind, side };
}

/** Just what fiducialChoices reads of a job. */
function job(copies: string[], side_up: "top" | "bottom" = "top"): Job {
  return {
    side_up,
    board: {
      placements: [mark("FID1", "top"), mark("R1", "top", "part"), mark("FID2", "bottom")],
    },
    panel: {
      copies: copies.map((id) => ({ id })),
      fiducials: [mark("KiKit_FID_T_1", "top"), mark("KiKit_FID_B_1", "bottom")],
    },
  } as unknown as Job;
}

describe("fiducialChoices", () => {
  it("lists a single board's fiducials by ref, then the panel's", () => {
    expect(fiducialChoices(job(["1"])).map((c) => [c.label, c.ref, c.copyId])).toEqual([
      ["FID1", "FID1", "1"],
      ["KiKit_FID_T_1 on the panel", "KiKit_FID_T_1", null],
    ]);
  });

  it("names the board copy when the panel has several", () => {
    expect(fiducialChoices(job(["1", "2"])).map((c) => c.label)).toEqual([
      "FID1 on board 1",
      "FID1 on board 2",
      "KiKit_FID_T_1 on the panel",
    ]);
  });

  it("offers only the side facing up", () => {
    expect(fiducialChoices(job(["1"], "bottom")).map((c) => c.ref)).toEqual([
      "FID2",
      "KiKit_FID_B_1",
    ]);
  });
});
