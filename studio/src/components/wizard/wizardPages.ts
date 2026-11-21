import type { WizardState } from "../../api/types";

/** What each of the wizard's stops asks of the owner, and what Start will do. */
export interface StopText {
  title: string;
  /** What the owner does first: the one thing the machine cannot do or check itself. */
  ask: string;
  /** What Start runs, in order. */
  runs: string;
}

export const STOP_TEXT: Record<0 | 1 | 2 | 3, StopText> = {
  0: {
    title: "Choose your machine",
    ask: "Which machine this is, the serial port it is on, and its cameras.",
    runs: "",
  },
  1: {
    title: "Nozzles level, hand on the power",
    ask:
      "Fit the nozzle tips, empty, and check the nozzles are level. Keep a hand on the " +
      "machine's power: unplugging the controller, or pressing its reset button, stops all " +
      "motion at once (as does an E-stop switch, if it has one).",
    runs:
      "Homes, then visual homing (which checks X, Y and the top camera), the top camera's " +
      "scale, the bottom camera, naming the tip on each nozzle, and the feeders. A few " +
      "minutes, at a quarter of full speed. Steps already done are skipped; Run again " +
      "redoes them all.",
  },
  2: {
    title: "Token on the datum board",
    ask:
      "Fit the N045 on one nozzle and the N08 on the other. Lay a calibration token flat " +
      "on the datum board, opening up, clear of the center dot. Without a token, a small " +
      "paper disc from a hole punch will do.",
    runs:
      "The top camera finds the token by its opening (or the paper disc). Each nozzle touches the datum board to find its " +
      "height, then picks up the disc, turns it and sets it down three times, measuring its " +
      "vacuum on the way. About a minute per nozzle. For a tip fitted since, too big for " +
      "the disc, lay a square of plain paper (25 mm or so) on the datum board instead: " +
      "only the new tip's height and vacuum are measured. Then each new tip turns over the " +
      "bottom camera, to measure how far its end is off the axis it turns about. Change " +
      "tips does that too, once the bottom camera is calibrated.",
  },
  3: {
    title: "Discard and park",
    ask:
      "Check where rejected parts are dropped and where the head waits, on the map; move " +
      "them if they are not right.",
    runs: "",
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
