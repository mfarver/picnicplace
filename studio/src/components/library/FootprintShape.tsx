import type { Footprint, Pad } from "../../api/types";
import { footprintExtent } from "./footprint";

/**
 * A footprint, drawn in footprint millimeters (+Y up, so Y is negated for SVG), the one
 * way every view draws it: the body outline dashed, pads outlined (so whatever is under
 * them stays visible), the pin-1 pad filled red and drawn last, and a small "+" at the
 * origin. Put it inside a <g> that places and turns it. With `onPad`, pads can be
 * clicked (to mark pin 1).
 */
export function FootprintShape({
  footprint,
  tone,
  onPad,
}: {
  footprint: Footprint;
  /** "danger" draws the whole footprint in red, such as a part that does not fit. */
  tone?: "danger";
  onPad?: (index: number) => void;
}) {
  const ext = footprintExtent(footprint);
  const plus = Math.min(ext.x, ext.y) * 0.25;
  const order = footprint.pads
    .map((pad, index) => ({ pad, index }))
    .sort((a, b) => Number(a.pad.pin1) - Number(b.pad.pin1));
  return (
    <g className={`fp-shape${tone ? ` fp-${tone}` : ""}${onPad ? " fp-clickable" : ""}`}>
      {footprint.body && (
        <rect
          className="fp-body"
          x={-footprint.body.x / 2}
          y={-footprint.body.y / 2}
          width={footprint.body.x}
          height={footprint.body.y}
        />
      )}
      {order.map(({ pad, index }) => (
        <PadShape key={index} pad={pad} onClick={onPad ? () => onPad(index) : undefined} />
      ))}
      <g className="fp-plus">
        <line x1={-plus} y1={0} x2={plus} y2={0} />
        <line x1={0} y1={-plus} x2={0} y2={plus} />
      </g>
    </g>
  );
}

function PadShape({ pad, onClick }: { pad: Pad; onClick?: () => void }) {
  const r = (Math.min(pad.width, pad.height) / 2) * (pad.roundness / 100);
  return (
    <rect
      className={pad.pin1 ? "fp-pad fp-pad-1" : "fp-pad"}
      x={-pad.width / 2}
      y={-pad.height / 2}
      width={pad.width}
      height={pad.height}
      rx={r}
      transform={`translate(${pad.x} ${-pad.y}) rotate(${-pad.rotation})`}
      onClick={onClick}
    >
      <title>{`Pad ${pad.name}${pad.pin1 ? " (pin 1)" : ""}`}</title>
    </rect>
  );
}

/** A footprint on its own, scaled to fit. With `onPad`, pads can be clicked. */
export function FootprintDrawing({
  footprint,
  onPad,
}: {
  footprint: Footprint;
  onPad?: (index: number) => void;
}) {
  const ext = footprintExtent(footprint);
  const half = Math.max(ext.x, ext.y) * 1.25;
  return (
    <svg
      className="fp-drawing"
      viewBox={`${-half} ${-half} ${2 * half} ${2 * half}`}
      role="img"
      aria-label={`Footprint ${footprint.id}`}
    >
      <FootprintShape footprint={footprint} onPad={onPad} />
    </svg>
  );
}
