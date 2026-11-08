import type { Footprint, Tape } from "../../api/types";
import { footprintExtent } from "./footprint";
import { FootprintShape } from "./FootprintShape";

/** EIA-481: from the sprocket holes' center line to the pockets' center line, by width. */
const POCKET_LINE: Record<number, number> = {
  8: 3.5,
  12: 5.5,
  16: 7.5,
  24: 11.5,
  32: 14.2,
  44: 20.2,
  56: 26.2,
};
const SPROCKET_EDGE = 1.75;
const SPROCKET_PITCH = 4;
const SPROCKET_D = 1.5;
const POCKETS = 3;

/**
 * A short length of tape in the EIA-481 view: looking down on the cover tape, sprocket
 * holes along the top, feeding left to right. Each pocket holds the part at its
 * rotation in tape, so the drawing can be checked against the reel or the datasheet.
 */
export function TapeDrawing({ tape, footprint }: { tape: Tape; footprint?: Footprint }) {
  const width = tape.width && POCKET_LINE[Math.round(tape.width)] ? Math.round(tape.width) : 8;
  const pitch = tape.pitch ?? 4;
  const pocketY = SPROCKET_EDGE + POCKET_LINE[width]!;
  const length = Math.max(3 * SPROCKET_PITCH, POCKETS * pitch);
  const ext = footprint ? footprintExtent(footprint) : { x: 0.8, y: 0.4 };
  const turned = rotatedExtent(ext, tape.rotation);
  const pocket = { x: turned.x * 2 + 0.4, y: turned.y * 2 + 0.4 };
  const holes = Math.floor(length / SPROCKET_PITCH) + 1;
  const quadrant = tape.pin1_quadrant ? quadrantCenter(tape.pin1_quadrant, pocket) : null;

  return (
    <svg
      className="tape-drawing"
      viewBox={`-1 -1.5 ${length + 4} ${width + 3}`}
      role="img"
      aria-label="The part in its tape"
    >
      <rect className="tape-body" x={0} y={0} width={length + 2} height={width} />
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
        const cx = 2 + i * pitch;
        return (
          <g key={i} transform={`translate(${cx} ${pocketY})`}>
            <rect
              className="tape-pocket"
              x={-pocket.x / 2}
              y={-pocket.y / 2}
              width={pocket.x}
              height={pocket.y}
            />
            {quadrant && i === 0 && (
              <circle className="tape-quadrant" cx={quadrant.x} cy={quadrant.y} r={0.25} />
            )}
            {footprint && (
              <g transform={`rotate(${-tape.rotation})`}>
                <FootprintShape footprint={footprint} />
              </g>
            )}
          </g>
        );
      })}
      <g className="tape-feed" transform={`translate(${length + 1.5} ${width + 0.9})`}>
        <line x1={-3} y1={0} x2={0} y2={0} />
        <path d="M0 0 L-0.8 -0.45 L-0.8 0.45 Z" />
      </g>
    </svg>
  );
}

function rotatedExtent(ext: { x: number; y: number }, degrees: number) {
  const a = (degrees * Math.PI) / 180;
  const c = Math.abs(Math.cos(a));
  const s = Math.abs(Math.sin(a));
  return { x: ext.x * c + ext.y * s, y: ext.x * s + ext.y * c };
}

/** Where the datasheet's pin-1 quadrant is in a pocket (SVG Y down: Q1 is top left). */
function quadrantCenter(q: string, pocket: { x: number; y: number }) {
  const dx = pocket.x / 4;
  const dy = pocket.y / 4;
  return (
    {
      Q1: { x: -dx, y: -dy },
      Q2: { x: dx, y: -dy },
      Q3: { x: -dx, y: dy },
      Q4: { x: dx, y: dy },
    }[q] ?? { x: 0, y: 0 }
  );
}
