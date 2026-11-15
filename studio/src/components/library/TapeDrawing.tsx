import type { Footprint, Tape } from "../../api/types";
import {
  POCKET_TO_SPROCKET,
  QUADRANT_PLACE,
  SPROCKET_D,
  SPROCKET_EDGE,
  SPROCKET_PITCH,
  fitCheck,
  halfExtent,
  pin1Quadrant,
  pocketLine,
  turnedSize,
} from "../../tape";
import { FootprintShape } from "./FootprintShape";

const POCKETS = 3;
const ARROW = 0.5;

/**
 * A short length of tape in the EIA-481 view (looking down on the cover tape, sprocket
 * holes along the top, feeding left to right), with the footprint in each pocket at the
 * rotation in tape, so a wrong rotation or a part too big for the tape shows at once.
 * Pin 1's quadrant of each pocket is shaded red. Under it: whether the part fits, and what
 * is missing.
 */
export function TapeDrawing({ tape, footprint }: { tape: Tape; footprint?: Footprint }) {
  const width = tape.width !== null && pocketLine(tape.width) !== null ? tape.width : 8;
  const line = pocketLine(width)!;
  const pitch = tape.pitch ?? 4;
  const rotation = tape.rotation ?? 0;
  const pocketY = SPROCKET_EDGE + line;
  const hasPads = !!footprint && footprint.pads.length > 0;
  const fit = hasPads ? fitCheck(footprint, tape) : null;
  const size = hasPads
    ? turnedSize(
        { x: 2 * halfExtent(footprint).x, y: 2 * halfExtent(footprint).y },
        rotation,
      )
    : { x: 1.6, y: 0.8 };
  const room = fit?.room ?? { x: Math.max(size.x + 0.4, 1), y: Math.max(size.y + 0.4, 1) };
  // The first pocket sits P2 past a sprocket hole, far enough in that it stays on the tape.
  const skip = Math.max(0, Math.ceil((room.x / 2 + 0.5 - POCKET_TO_SPROCKET) / SPROCKET_PITCH));
  const first = POCKET_TO_SPROCKET + skip * SPROCKET_PITCH;
  const length = Math.max(3 * SPROCKET_PITCH, first + POCKETS * pitch);
  const holes = Math.floor(length / SPROCKET_PITCH) + 1;
  const quadrant = hasPads ? pin1Quadrant(footprint, rotation) : null;
  const marked = hasPads ? footprint.pads.filter((p) => p.pin1).length : 0;
  const tooBig = fit !== null && !fit.fits;
  const mm = (v: number) => v.toFixed(1);
  const chainY = width + 1.4;
  const widthX = length + 3.4;

  const notes: { text: string; tone?: "danger" | "warn" }[] = [];
  if (tape.width === null || tape.pitch === null) {
    notes.push({
      text: "Give the tape's width and pitch to check the part fits; drawn as 8 mm at 4 mm.",
      tone: "warn",
    });
  } else if (pocketLine(tape.width) === null) {
    notes.push({ text: `${tape.width} mm is not an EIA-481 width; drawn as 8 mm.`, tone: "warn" });
  }
  if (tape.rotation === null) {
    notes.push({ text: "The rotation in tape is not known: drawn at 0°.", tone: "warn" });
  }
  if (!footprint) notes.push({ text: "No footprint to draw." });
  else if (!hasPads) notes.push({ text: `Footprint ${footprint.id} has no pads to draw.` });
  else if (marked === 0) {
    notes.push({
      text: "No pad is marked as pin 1: mark one under Footprints to see which way it faces.",
    });
  } else if (!quadrant && marked === 1) {
    notes.push({ text: "Pin 1 is on the pocket's center line, so no quadrant: check its side." });
  }
  if (fit) {
    const what = `the part is ${mm(fit.part.x)} × ${mm(fit.part.y)} mm`;
    const roomText =
      `${tape.width} mm tape at ${tape.pitch} mm pitch has room for about ` +
      `${mm(fit.room.x)} × ${mm(fit.room.y)} mm`;
    notes.push(
      fit.fits
        ? { text: `Fits: ${what}; ${roomText}.` }
        : { text: `Doesn't fit: ${what}; ${roomText}.`, tone: "danger" },
    );
  }

  return (
    <>
      <svg
        className="tape-drawing"
        viewBox={`-2.5 -1.2 ${length + 8} ${width + 4.4}`}
        role="img"
        aria-label="The part in its tape"
      >
        <rect className="tape-body" x={-2} y={0} width={length + 4} height={width} />
        {Array.from({ length: holes }, (_, i) => (
          <circle
            key={i}
            className="tape-hole"
            cx={i * SPROCKET_PITCH}
            cy={SPROCKET_EDGE}
            r={SPROCKET_D / 2}
          />
        ))}
        {Array.from({ length: POCKETS }, (_, i) => {
          const cx = first + i * pitch;
          const showPart = hasPads && (!tooBig || i === 1);
          return (
            <g key={i} transform={`translate(${cx} ${pocketY})`}>
              <rect
                className={fit ? "tape-room" : "tape-pocket"}
                x={-room.x / 2}
                y={-room.y / 2}
                width={room.x}
                height={room.y}
              />
              {quadrant && (
                <rect
                  className="tape-pin1"
                  x={quadrant === "Q1" || quadrant === "Q3" ? -room.x / 2 : 0}
                  y={quadrant === "Q1" || quadrant === "Q2" ? -room.y / 2 : 0}
                  width={room.x / 2}
                  height={room.y / 2}
                >
                  <title>
                    {`Pin 1 is in ${quadrant} (${QUADRANT_PLACE[quadrant]}) of the pocket`}
                  </title>
                </rect>
              )}
              {showPart && (
                <g transform={`rotate(${-rotation})`}>
                  <FootprintShape footprint={footprint} tone={tooBig ? "danger" : undefined} />
                </g>
              )}
            </g>
          );
        })}
        {/* P1: a chain of dimensions under the tape, pocket center to pocket center. */}
        <g className="tape-dim">
          {Array.from({ length: POCKETS }, (_, i) => (
            <line
              key={i}
              className="tape-ext"
              x1={first + i * pitch}
              y1={pocketY + room.y / 2 + 0.2}
              x2={first + i * pitch}
              y2={chainY + 0.5}
            />
          ))}
          {Array.from({ length: POCKETS - 1 }, (_, i) => (
            <Dimension
              key={i}
              from={{ x: first + i * pitch, y: chainY }}
              to={{ x: first + (i + 1) * pitch, y: chainY }}
              label={mmLabel(tape.pitch)}
            />
          ))}
        </g>
        {/* W: along the right end. */}
        <g className="tape-dim">
          <line className="tape-ext" x1={length + 2.2} y1={0} x2={widthX + 0.5} y2={0} />
          <line className="tape-ext" x1={length + 2.2} y1={width} x2={widthX + 0.5} y2={width} />
          <Dimension
            from={{ x: widthX, y: 0 }}
            to={{ x: widthX, y: width }}
            label={mmLabel(tape.width)}
          />
        </g>
        <g className="tape-feed" transform={`translate(${length + 1.5} -0.6)`}>
          <line x1={-3} y1={0} x2={0} y2={0} />
          <path d="M0 0 L-0.8 -0.45 L-0.8 0.45 Z" />
          <text x={-3.4} y={0.35}>
            feed
          </text>
        </g>
      </svg>
      {notes.map((n, i) => (
        <p
          key={i}
          className={`caption${n.tone ? ` text-${n.tone}` : ""}`}
        >
          {n.text}
        </p>
      ))}
    </>
  );
}

/** "8 mm", or "? mm" for a value not known. */
function mmLabel(value: number | null): string {
  return `${value ?? "?"} mm`;
}

/**
 * A dimension: a line between two points (level or plumb) with an arrowhead at each end
 * and its label beside the middle (under a level line, right of a plumb one, turned to
 * read along it).
 */
function Dimension({
  from,
  to,
  label,
}: {
  from: { x: number; y: number };
  to: { x: number; y: number };
  label: string;
}) {
  const level = from.y === to.y;
  const length = level ? to.x - from.x : to.y - from.y;
  const head = (at: { x: number; y: number }, sign: number) => {
    const a = sign * ARROW;
    const w = ARROW * 0.4;
    return level
      ? `M${at.x} ${at.y} L${at.x + a} ${at.y - w} L${at.x + a} ${at.y + w} Z`
      : `M${at.x} ${at.y} L${at.x - w} ${at.y + a} L${at.x + w} ${at.y + a} Z`;
  };
  const mid = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
  return (
    <g>
      <line x1={from.x} y1={from.y} x2={to.x} y2={to.y} />
      <path d={head(from, Math.sign(length))} />
      <path d={head(to, -Math.sign(length))} />
      {level ? (
        <text x={mid.x} y={mid.y + 0.8}>
          {label}
        </text>
      ) : (
        <text transform={`translate(${mid.x + 0.7} ${mid.y}) rotate(-90)`}>{label}</text>
      )}
    </g>
  );
}
