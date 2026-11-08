import type { Footprint, Pad } from "../../api/types";
import { footprintExtent } from "./footprint";

/**
 * A footprint's pads and body, drawn in footprint millimeters (+Y up, so Y is negated
 * for SVG). Put it inside a <g> that places and turns it.
 */
export function FootprintShape({
  footprint,
  pin1 = true,
}: {
  footprint: Footprint;
  pin1?: boolean;
}) {
  const first = pin1 ? pinOne(footprint.pads) : undefined;
  return (
    <g className="fp-shape">
      {footprint.body && (
        <rect
          className="fp-body"
          x={-footprint.body.x / 2}
          y={-footprint.body.y / 2}
          width={footprint.body.x}
          height={footprint.body.y}
        />
      )}
      {footprint.pads.map((pad, i) => (
        <PadShape key={i} pad={pad} first={pad === first} />
      ))}
    </g>
  );
}

function PadShape({ pad, first }: { pad: Pad; first: boolean }) {
  const r = (Math.min(pad.width, pad.height) / 2) * (pad.roundness / 100);
  return (
    <rect
      className={first ? "fp-pad fp-pad-1" : "fp-pad"}
      x={-pad.width / 2}
      y={-pad.height / 2}
      width={pad.width}
      height={pad.height}
      rx={r}
      transform={`translate(${pad.x} ${-pad.y}) rotate(${-pad.rotation})`}
    >
      <title>{`Pad ${pad.name}`}</title>
    </rect>
  );
}

function pinOne(pads: Pad[]): Pad | undefined {
  return pads.find((p) => p.name === "1") ?? pads.find((p) => p.name.toUpperCase() === "A1");
}

/** A footprint on its own, scaled to fit, with its origin and axes marked. */
export function FootprintDrawing({ footprint }: { footprint: Footprint }) {
  const ext = footprintExtent(footprint);
  const half = Math.max(ext.x, ext.y) * 1.25;
  const tick = half * 0.18;
  return (
    <svg
      className="fp-drawing"
      viewBox={`${-half} ${-half} ${2 * half} ${2 * half}`}
      role="img"
      aria-label={`Footprint ${footprint.id}`}
    >
      <FootprintShape footprint={footprint} />
      <g className="fp-origin">
        <line x1={0} y1={0} x2={tick} y2={0} />
        <line x1={0} y1={0} x2={0} y2={-tick} />
      </g>
    </svg>
  );
}
