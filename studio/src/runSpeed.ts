import type { RunState } from "./api/types";

/** Placements per hour (CPH) while the machine worked, and over the whole run from its
 * start to its end (or now), the pauses for the operator included. */
export function placementsPerHour(
  run: Pick<RunState, "stats" | "working_since" | "started_at" | "finished_at">,
  now: number = Date.now(),
) {
  const since = run.working_since ? now - Date.parse(run.working_since) : 0;
  const working = run.stats.working_s + Math.max(0, since) / 1000;
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  const total = (end - Date.parse(run.started_at)) / 1000;
  const rate = (s: number) => (s > 0 ? Math.round((run.stats.placed * 3600) / s) : null);
  return { working: rate(working), total: rate(total) };
}
