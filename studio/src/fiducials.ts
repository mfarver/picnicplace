import type { Job } from "./api/types";

/** A fiducial the camera can be centered on to say where the job is: on a board copy, or
 * (with no copy) on the panel. */
export interface FiducialChoice {
  key: string;
  ref: string;
  copyId: string | null;
  label: string;
}

/** The job's fiducials on the side facing up: each board copy's, then the panel's. */
export function fiducialChoices(job: Job): FiducialChoice[] {
  const many = job.panel.copies.length > 1;
  const onBoard = job.board.placements.filter(
    (p) => p.kind === "fiducial" && p.side === job.side_up,
  );
  const choices: FiducialChoice[] = [];
  for (const copy of job.panel.copies) {
    for (const p of onBoard) {
      choices.push({
        key: `${copy.id}:${p.ref}`,
        ref: p.ref,
        copyId: copy.id,
        label: many ? `${p.ref} on board ${copy.id}` : p.ref,
      });
    }
  }
  for (const p of job.panel.fiducials.filter((f) => f.side === job.side_up)) {
    choices.push({
      key: `panel:${p.ref}`,
      ref: p.ref,
      copyId: null,
      label: `${p.ref} on the panel`,
    });
  }
  return choices;
}
