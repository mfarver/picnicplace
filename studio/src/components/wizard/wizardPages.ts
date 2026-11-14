import type { WizardPage, WizardState } from "../../api/types";

/** What a wizard page tells the owner: the first three of its five parts. */
export interface PageText {
  /** What this learns, and why the machine needs it. */
  learns: string;
  /** What the owner must do or have ready; Start stays off until each is ticked. */
  before: string[];
  /** What the machine will move and touch, in order, and how long it takes. */
  happens: string;
}

export const PAGE_TEXT: Record<WizardPage["id"], PageText> = {
  welcome: {
    learns:
      "This wizard takes the machine from unpacked to calibrated, one page at a time. You " +
      "can leave at any page and pick up there later. Until it is finished, the machine " +
      "only homes, jogs slowly and runs these steps: picks, placements and runs wait.",
    before: [],
    happens: "",
  },
  machine: {
    learns: "Which machine this is, so PicnicPlace knows its axes, cameras and feeders.",
    before: [],
    happens: "Chosen in the config for now; choosing it here comes in a later version.",
  },
  connect: {
    learns:
      "Which serial port the machine is on, and which firmware it runs, so moves and " +
      "readings go to the right place.",
    before: [],
    happens: "For now, check this in the bring-up checklist below this panel.",
  },
  cameras: {
    learns: "Which camera is the top one and which the bottom, and that the top camera's " +
      "picture moves the way the head does.",
    before: [],
    happens: "For now, check this in the bring-up checklist below this panel.",
  },
  first_motion: {
    learns:
      "That the machine homes, each axis moves the way the profile says, Z moves the right " +
      "nozzle, the pumps and valves are the right way around, and Stop works.",
    before: [],
    happens: "For now, check each of these in the bring-up checklist below this panel.",
  },
  bed: {
    learns:
      "Where boards cannot go on the bed (the datum board, the bottom camera's opening), " +
      "so a job is never laid out over them.",
    before: [],
    happens: "For now, record these in the bring-up checklist below this panel.",
  },
  visual_homing: {
    learns:
      "Where the datum board's center dot really is. Homing switches are only good to a " +
      "few tenths of a millimeter; the dot puts every position after it to within a few " +
      "hundredths.",
    before: ["The datum board is fitted, and its center dot is clean.", "The machine is homed."],
    happens:
      "The head moves the top camera over where the dot should be, finds it, and corrects " +
      "X and Y. A few seconds.",
  },
  top_camera_scale: {
    learns:
      "How many millimeters each pixel of the top camera covers, so what the camera sees " +
      "can be measured.",
    before: ["Visual homing is done (the page before)."],
    happens:
      "The head steps the camera a known distance each way over the datum board's dot, and " +
      "measures how far the dot moves in the picture. Under a minute.",
  },
  nozzle_tips: {
    learns:
      "Which tip is on each nozzle. Each tip has its own height and vacuum levels, kept " +
      "for when it is fitted again.",
    before: [],
    happens: "Say which tip is on each nozzle below, then confirm.",
  },
  bottom_camera: {
    learns:
      "Where the bottom camera is, the height it is in focus at, its scale, and how far its " +
      "picture is turned, so parts can be aligned on the nozzle.",
    before: [
      "The nozzle tips are empty.",
      "The tips said on the nozzle tips page are the ones fitted.",
      "The bottom camera's light works.",
    ],
    happens:
      "The left nozzle moves over the camera and lowers to near its focus height (never " +
      "more than 2 mm past it), steps up and down for the sharpest picture, then steps " +
      "the head each way. About a minute.",
  },
  nozzle_offsets: {
    learns:
      "Where each nozzle really is from the top camera, how far down its tip reaches, and " +
      "what its vacuum reads open, sealed and holding a part. Every pick and placement " +
      "depends on these.",
    before: [
      "Punch a small paper disc (a hole punch is right).",
      "Lay it flat on the datum board, clear of the center dot.",
      "Center the top camera on the disc.",
      "The tips said on the nozzle tips page are the ones fitted.",
    ],
    happens:
      "The camera finds the disc. Then each nozzle touches the datum board to find its " +
      "height, and picks up the disc, turns it and sets it down three times. About a minute " +
      "per nozzle.",
  },
  nozzle_runout: {
    learns: "How far each nozzle's tip wobbles as it turns.",
    before: [],
    happens: "Not built yet: taken as none. This page is skipped for now.",
  },
  discard_park: {
    learns:
      "Where rejected parts are dropped, and where the head waits out of the way between " +
      "jobs.",
    before: [],
    happens:
      "Check each location below. To move one, jog the top camera over the place and set " +
      "it. Then confirm.",
  },
  feeders: {
    learns:
      "Where each feeder rail really is, from the fiducials on its feeders, so picks start " +
      "in the right place.",
    before: ["The feeders are on the rails, and scanned (Feeders workspace)."],
    happens: "In the Feeders workspace, Calibrate rails. This page is done when they fit.",
  },
  strips_trays: {
    learns: "Where any strips or trays on the bed are, and their first part.",
    before: [],
    happens:
      "Teach them in the Feeders workspace now, or skip this page and teach them later.",
  },
  done: {
    learns: "Everything is calibrated: the machine is no longer limited.",
    before: [],
    happens: "",
  },
};

/** What the machine's stage means for the owner, in a sentence. */
export function stageMessage(state: WizardState): string {
  switch (state.stage) {
    case "calibrated":
      return "The machine is calibrated.";
    case "out_of_date":
      return "Calibration is out of date: runs are paused until the flagged pages are redone.";
    default:
      return "Calibration is not complete: some machine functions are limited.";
  }
}
