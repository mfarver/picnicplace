import type { JobLayout } from "../../api/types";

/**
 * A job drawn on the machine map: the panel outline, each placement as a dot, and the
 * fiducials as rings. Drawn in the map's coordinates (machine X, machine Y negated).
 */
export function JobOverlay({ layout }: { layout: JobLayout }) {
  if (layout.outline.length === 0) return null;
  const bad = new Set(
    layout.issues.filter((i) => i.ref).map((i) => `${i.copy_id ?? ""}:${i.ref ?? ""}`),
  );
  const points = layout.outline.map((p) => `${p.x},${-p.y}`).join(" ");
  const covers = layout.issues.some((i) => i.severity === "error" && !i.ref);
  return (
    <g className="job-overlay">
      <polygon className={covers ? "job-outline job-outline-bad" : "job-outline"} points={points}>
        <title>The board (panel) outline</title>
      </polygon>
      {layout.placements.map((p) => {
        const key = `${p.copy_id ?? ""}:${p.ref}`;
        const cls = bad.has(key) ? "job-bad" : p.enabled ? "job-ok" : "job-off";
        return p.kind === "fiducial" ? (
          <circle key={key} className={`job-fiducial ${cls}`} cx={p.x} cy={-p.y} r={1.6}>
            <title>{`${p.ref}${p.copy_id ? ` (board ${p.copy_id})` : " (panel)"}`}</title>
          </circle>
        ) : (
          <circle key={key} className={`job-part ${cls}`} cx={p.x} cy={-p.y} r={0.7}>
            <title>{`${p.ref} ${p.part_id ?? ""}${p.copy_id ? ` (board ${p.copy_id})` : ""}${p.note ? `: ${p.note}` : ""}`}</title>
          </circle>
        );
      })}
    </g>
  );
}
