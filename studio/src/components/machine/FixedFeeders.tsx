import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { FixedFeederState, Footprint } from "../../api/types";
import { FootprintShape } from "../library/FootprintShape";
import { footprintExtent } from "../library/footprint";
import { partPosition, remainingText, stripTape } from "./fixedFeederMath";
import { Label } from "./MachineView";

/**
 * Strips and trays held on the bed, for the machine map: an outline around the parts,
 * each part's place (filled while it still holds a part), and how many are left. When
 * the part's footprint is known, drawn to scale: a strip's tape at its width with its
 * sprocket holes, and each part's footprint as it lies, pin 1's pad and corner in red,
 * so it can be held up against the camera's view.
 */
export function FixedFeederShapes({
  fixed,
  selected,
}: {
  fixed: FixedFeederState[];
  selected?: string | null;
}) {
  const footprints = useFootprints(fixed);
  return (
    <g className="ff-layer">
      {fixed.map((f) => (
        <FixedFeederShape
          key={f.id}
          state={f}
          selected={f.id === selected}
          footprint={f.feeder.part_id ? footprints[f.feeder.part_id] : undefined}
        />
      ))}
    </g>
  );
}

/** The footprints of the parts the strips and trays hold, by part ID. */
function useFootprints(fixed: FixedFeederState[]): Record<string, Footprint> {
  const ids = [...new Set(fixed.map((f) => f.feeder.part_id ?? "").filter(Boolean))]
    .sort()
    .join("\n");
  const [found, setFound] = useState<Record<string, Footprint>>({});
  useEffect(() => {
    if (!ids) return;
    let cancelled = false;
    const fetchOne = async (partId: string): Promise<[string, Footprint] | null> => {
      const part = await api.GET("/api/v1/library/parts/{part_id}", {
        params: { path: { part_id: partId } },
      });
      const footprintId = part.data?.footprint_id;
      if (!footprintId) return null;
      const shape = await api.GET("/api/v1/library/footprints/{footprint_id}", {
        params: { path: { footprint_id: footprintId } },
      });
      return shape.data && shape.data.pads.length > 0 ? [partId, shape.data] : null;
    };
    void Promise.all(ids.split("\n").map(fetchOne)).then((pairs) => {
      if (!cancelled) setFound(Object.fromEntries(pairs.filter((p) => p !== null)));
    });
    return () => {
      cancelled = true;
    };
  }, [ids]);
  return found;
}

const MAX_DRAWN = 2000;

function FixedFeederShape({
  state,
  selected,
  footprint,
}: {
  state: FixedFeederState;
  selected: boolean;
  footprint?: Footprint;
}) {
  const f = state.feeder;
  const capacity = f.columns * f.rows;
  const along = Math.hypot(f.step.x, f.step.y);
  const across = Math.hypot(f.row_step.x, f.row_step.y);
  const pitch = Math.min(along || 4, f.rows > 1 ? across || 4 : Infinity);
  const r = Math.max(0.4, Math.min(1.6, pitch / 3));
  // Footprints are drawn only when it is known which way the parts lie.
  const shape = footprint && state.angle !== null ? footprint : undefined;
  const ext = shape ? footprintExtent(shape) : null;
  const tape = stripTape(f);

  // The outline: a strip's tape, or the grid's corners half a step out each way. One
  // row: a band as wide as the step is long, across the row.
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
  const corners = tape?.outline ?? [
    at(-0.5, -0.5),
    at(c + 0.5, -0.5),
    at(c + 0.5, n + 0.5),
    at(-0.5, n + 0.5),
  ];
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
    const taken = i < f.picked;
    parts.push(
      shape && ext ? (
        <g
          key={i}
          className={taken ? "ff-fp ff-taken" : "ff-fp"}
          transform={`translate(${p.x} ${-p.y}) rotate(${-(state.angle ?? 0)})`}
        >
          <Pin1Corner footprint={shape} ext={ext} />
          <FootprintShape footprint={shape} />
        </g>
      ) : (
        <circle
          key={i}
          className={taken ? "ff-part ff-taken" : "ff-part"}
          cx={p.x}
          cy={-p.y}
          r={r}
        />
      ),
    );
  }
  const nextR = ext ? Math.hypot(ext.x, ext.y) + 0.6 : r + 1;

  return (
    <g className={`ff ${status}${selected ? " ff-selected" : ""}`}>
      <title>
        {`${state.id}${f.label ? ` (${f.label})` : ""}: ` +
          `${f.kind === "strip" ? "strip of tape" : "tray"}` +
          `${f.part_id ? ` of ${f.part_id}` : ""}, ${remainingText(state)}`}
      </title>
      <polygon className="ff-outline" points={outline} />
      {tape?.holes.map((h, i) => (
        <circle key={`h${i}`} className="ff-hole" cx={h.x} cy={-h.y} r={0.75} />
      ))}
      {parts}
      {state.next && (
        <circle className="ff-next" cx={state.next.x} cy={-state.next.y} r={nextR} />
      )}
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

/** The quarter of the part that holds pin 1, shaded, in footprint millimeters. */
function Pin1Corner({ footprint, ext }: { footprint: Footprint; ext: { x: number; y: number } }) {
  const marked = footprint.pads.filter((p) => p.pin1);
  const pin = marked.length === 1 ? marked[0]! : null;
  if (!pin || Math.abs(pin.x) < 1e-6 || Math.abs(pin.y) < 1e-6) return null;
  return (
    <rect
      className="ff-pin1"
      x={pin.x < 0 ? -ext.x : 0}
      y={pin.y > 0 ? -ext.y : 0}
      width={ext.x}
      height={ext.y}
    />
  );
}
