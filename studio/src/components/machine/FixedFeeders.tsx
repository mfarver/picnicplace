import type { FixedFeederState } from "../../api/types";
import { partPosition, remainingText } from "./fixedFeederMath";
import { Label } from "./MachineView";

/**
 * Strips and trays held on the bed, for the machine map: an outline around the parts,
 * each part's place (filled while it still holds a part), and how many are left.
 */
export function FixedFeederShapes({
  fixed,
  selected,
}: {
  fixed: FixedFeederState[];
  selected?: string | null;
}) {
  return (
    <g className="ff-layer">
      {fixed.map((f) => (
        <FixedFeederShape key={f.id} state={f} selected={f.id === selected} />
      ))}
    </g>
  );
}

const MAX_DRAWN = 2000;

function FixedFeederShape({ state, selected }: { state: FixedFeederState; selected: boolean }) {
  const f = state.feeder;
  const capacity = f.columns * f.rows;
  const along = Math.hypot(f.step.x, f.step.y);
  const across = Math.hypot(f.row_step.x, f.row_step.y);
  const pitch = Math.min(along || 4, f.rows > 1 ? across || 4 : Infinity);
  const r = Math.max(0.4, Math.min(1.6, pitch / 3));

  // The outline: the grid's corners, half a step out each way. One row: a band as wide
  // as the step is long, across the row.
  const u = along > 0 ? f.step : { x: 4, y: 0 };
  const v =
    f.rows > 1 && across > 0
      ? f.row_step
      : { x: (-u.y / Math.hypot(u.x, u.y)) * pitch, y: (u.x / Math.hypot(u.x, u.y)) * pitch };
  const c = f.columns - 1;
  const n = f.rows - 1;
  const at = (s: number, t: number) => ({
    x: f.first.x + s * u.x + t * v.x,
    y: f.first.y + s * u.y + t * v.y,
  });
  const corners = [at(-0.5, -0.5), at(c + 0.5, -0.5), at(c + 0.5, n + 0.5), at(-0.5, n + 0.5)];
  const outline = corners.map((p) => `${p.x},${-p.y}`).join(" ");
  const top = corners.reduce((a, b) => (b.y > a.y ? b : a));
  const xs = corners.map((p) => p.x);
  const label = `${state.id} · ${remainingText(state)}`;
  const status = !f.enabled
    ? "ff-off"
    : state.remaining === 0
      ? "ff-empty"
      : state.remaining * 5 <= capacity
        ? "ff-low"
        : "ff-ok";

  const parts = [];
  for (let i = 0; i < Math.min(capacity, MAX_DRAWN); i++) {
    const p = partPosition(f, i);
    parts.push(
      <circle
        key={i}
        className={i < f.picked ? "ff-part ff-taken" : "ff-part"}
        cx={p.x}
        cy={-p.y}
        r={r}
      />,
    );
  }

  return (
    <g className={`ff ${status}${selected ? " ff-selected" : ""}`}>
      <title>
        {`${state.id}${f.label ? ` (${f.label})` : ""}: ${f.kind === "strip" ? "strip of tape" : "tray"}` +
          `${f.part_id ? ` of ${f.part_id}` : ""}, ${remainingText(state)}`}
      </title>
      <polygon className="ff-outline" points={outline} />
      {parts}
      {state.next && <circle className="ff-next" cx={state.next.x} cy={-state.next.y} r={r + 1} />}
      <Label
        className="mv-label ff-label"
        x={(Math.min(...xs) + Math.max(...xs)) / 2}
        y={-top.y - 3}
        textAnchor="middle"
      >
        {label}
      </Label>
    </g>
  );
}
