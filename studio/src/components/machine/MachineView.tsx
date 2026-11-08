import { Maximize } from "lucide-react";
import {
  createContext,
  type PointerEvent,
  type SVGProps,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type {
  FeederBank,
  MachineProfile,
  MachineState,
  Point,
  StagingPlate,
} from "../../api/types";

/**
 * Scale overhead map of the machine (spec section 8.3).
 *
 * Drawn in machine millimeters with every Y negated (SVG's Y points down). The profile's
 * view_rotation then turns the drawing so the front of the machine is at the bottom of
 * the screen: on a LumenPnP (origin at the right rear) the whole drawing is rotated
 * 180°, and labels are turned back upright. Scroll to zoom, drag to pan.
 */

/** The profile's view rotation, for labels that must stay upright. */
const Rotation = createContext(0);

/** SVG text at a point in the drawing, kept upright whatever the view rotation. */
function Label({ x, y, ...rest }: SVGProps<SVGTextElement> & { x: number; y: number }) {
  const rotation = useContext(Rotation);
  return (
    <text
      x={x}
      y={y}
      transform={rotation ? `rotate(${rotation} ${x} ${y})` : undefined}
      {...rest}
    />
  );
}

/** Drawing coordinates (after the view rotation) to machine coordinates. */
function toMachine(p: { x: number; y: number }, rotation: number): Point {
  return rotation === 180 ? { x: -p.x, y: p.y } : { x: p.x, y: -p.y };
}

interface ViewBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

const MARGIN = 30;
const GRID = 50;
const MIN_WIDTH = 10;
const ZOOM_STEP = 1.15;
/** A press that moves less than this is a click, not a drag. */
const CLICK_SLOP_PX = 4;

/** Room kept clear on the right for the head inset, in screen pixels. */
const INSET_RESERVE_PX = 330;

/**
 * A view that shows the whole bed. Given the element's size, the bed is fitted into the
 * part of it left of the head inset when there is room for that.
 */
function fitView(d: MachineProfile, size?: { width: number; height: number }): ViewBox {
  const { bed } = d;
  const bw = bed.x_max - bed.x_min + 2 * MARGIN;
  const bh = bed.y_max - bed.y_min + 2 * MARGIN;
  // The bed's corners in drawing coordinates: (x, -y), or (-x, y) when rotated 180°.
  const rotated = d.view_rotation === 180;
  const bx = (rotated ? -bed.x_max : bed.x_min) - MARGIN;
  const by = (rotated ? bed.y_min : -bed.y_max) - MARGIN;
  if (!size || size.width <= 0 || size.height <= 0) return { x: bx, y: by, w: bw, h: bh };

  const reserve = size.width - INSET_RESERVE_PX > size.width * 0.5 ? INSET_RESERVE_PX : 0;
  const available = size.width - reserve;
  const scale = Math.min(available / bw, size.height / bh); // pixels per millimeter
  const w = size.width / scale;
  const h = size.height / scale;
  return { x: bx - (available / scale - bw) / 2, y: by - (h - bh) / 2, w, h };
}

function svgPoint(svg: SVGSVGElement, clientX: number, clientY: number, ctm?: DOMMatrix) {
  const matrix = ctm ?? svg.getScreenCTM()?.inverse();
  if (!matrix) return null;
  return new DOMPoint(clientX, clientY).matrixTransform(matrix);
}

interface Props {
  description: MachineProfile;
  machine?: MachineState;
  /** Moves are allowed right now. */
  canMove?: boolean;
  /** Move the head (top camera) to a point. Leave out to disable click-to-move. */
  onMoveTo?: (target: Point) => void;
}

export function MachineView({ description, machine, canMove = false, onMoveTo }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [view, setView] = useState<ViewBox>(() => fitView(description));
  const [cursor, setCursor] = useState<Point | null>(null);
  const drag = useRef<{
    start: DOMPoint;
    screen: Point;
    dragging: boolean;
    view: ViewBox;
    ctm: DOMMatrix;
  } | null>(null);
  const [target, setTarget] = useState<Point | null>(null);

  const userMoved = useRef(false);

  const fit = useCallback(() => {
    userMoved.current = false;
    const svg = svgRef.current;
    setView(fitView(description, svg ? svg.getBoundingClientRect() : undefined));
  }, [description]);

  // Keep the bed fitted as the element resizes, until the user zooms or pans.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const observer = new ResizeObserver(() => {
      if (!userMoved.current) setView(fitView(description, svg.getBoundingClientRect()));
    });
    observer.observe(svg);
    return () => observer.disconnect();
  }, [description]);

  // Wheel zoom needs a non-passive listener so the page does not scroll.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      userMoved.current = true;
      const p = svgPoint(svg, e.clientX, e.clientY);
      if (!p) return;
      const factor = e.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      setView((v) => {
        const maxW = fitView(description).w * 3;
        const w = Math.min(maxW, Math.max(MIN_WIDTH, v.w * factor));
        const f = w / v.w;
        return {
          x: p.x - (p.x - v.x) * f,
          y: p.y - (p.y - v.y) * f,
          w,
          h: v.h * f,
        };
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [description]);

  const onPointerDown = (e: PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM()?.inverse();
    if (!svg || !ctm || e.button !== 0) return;
    svg.setPointerCapture(e.pointerId);
    drag.current = {
      start: new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm),
      screen: { x: e.clientX, y: e.clientY },
      dragging: false,
      view,
      ctm,
    };
  };

  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const d = drag.current;
    if (d) {
      if (!d.dragging) {
        const moved = Math.hypot(e.clientX - d.screen.x, e.clientY - d.screen.y);
        if (moved < CLICK_SLOP_PX) return;
        d.dragging = true;
        userMoved.current = true;
      }
      const p = svgPoint(svg, e.clientX, e.clientY, d.ctm);
      if (p) {
        setView({ ...d.view, x: d.view.x - (p.x - d.start.x), y: d.view.y - (p.y - d.start.y) });
      }
      return;
    }
    const p = svgPoint(svg, e.clientX, e.clientY);
    if (p) setCursor(toMachine(p, description.view_rotation));
  };

  const onPointerUp = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.dragging || !onMoveTo) return;
    // A click, not a drag: pick a move target. Moving needs a second, explicit step.
    const svg = svgRef.current;
    const p = svg ? svgPoint(svg, e.clientX, e.clientY) : null;
    if (p) setTarget(toMachine(p, description.view_rotation));
  };

  const endDrag = () => {
    drag.current = null;
  };

  // Escape cancels a pending move target.
  useEffect(() => {
    if (!target) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setTarget(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [target]);

  const xAxis = description.axes.find((a) => a.name === "X");
  const yAxis = description.axes.find((a) => a.name === "Y");
  const inTravel = (p: Point) =>
    !!xAxis &&
    !!yAxis &&
    p.x >= xAxis.min &&
    p.x <= xAxis.max &&
    p.y >= yAxis.min &&
    p.y <= yAxis.max;
  const head =
    machine && machine.position.X !== undefined && machine.position.Y !== undefined
      ? { x: machine.position.X, y: machine.position.Y }
      : null;

  return (
    <div className="machine-view">
      <svg
        ref={svgRef}
        className="mv-svg"
        viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
        preserveAspectRatio="xMidYMid meet"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={endDrag}
        onPointerLeave={() => setCursor(null)}
        role="img"
        aria-label={`Overhead view of the ${description.name}`}
      >
        <Rotation.Provider value={description.view_rotation}>
          <g
            transform={
              description.view_rotation ? `rotate(${description.view_rotation})` : undefined
            }
          >
            <Grid description={description} />
            <rect
              className="mv-bed"
              x={description.bed.x_min}
              y={-description.bed.y_max}
              width={description.bed.x_max - description.bed.x_min}
              height={description.bed.y_max - description.bed.y_min}
              rx={4}
            />
            {description.staging_plates.map((plate) => (
              <PlateShape key={plate.id} plate={plate} />
            ))}
            {description.work_area && (
              <g>
                <rect
                  className="mv-work"
                  x={description.work_area.x_min}
                  y={-description.work_area.y_max}
                  width={description.work_area.x_max - description.work_area.x_min}
                  height={description.work_area.y_max - description.work_area.y_min}
                />
                <Label
                  className="mv-label mv-label-faint"
                  x={(description.work_area.x_min + description.work_area.x_max) / 2}
                  y={-(description.work_area.y_min + description.work_area.y_max) / 2}
                  textAnchor="middle"
                  dominantBaseline="middle"
                >
                  Board area
                </Label>
              </g>
            )}
            {xAxis && yAxis && (
              <rect
                className="mv-travel"
                x={xAxis.min}
                y={-yAxis.max}
                width={xAxis.max - xAxis.min}
                height={yAxis.max - yAxis.min}
              >
                <title>Travel limits</title>
              </rect>
            )}

            {description.feeder_banks.map((bank) => (
              <FeederBankShape key={bank.id} bank={bank} />
            ))}

            {description.cameras
              .filter((c) => c.mount === "fixed")
              .map((c) => (
                <g key={c.id} className="mv-camera">
                  <title>{c.label}</title>
                  <rect
                    x={c.position.x - c.field_of_view.x / 2}
                    y={-c.position.y - c.field_of_view.y / 2}
                    width={c.field_of_view.x}
                    height={c.field_of_view.y}
                  />
                  <circle cx={c.position.x} cy={-c.position.y} r={3} />
                  <Label
                    className="mv-label"
                    x={c.position.x}
                    y={-c.position.y + c.field_of_view.y / 2 + 8}
                    textAnchor="middle"
                  >
                    {c.label}
                  </Label>
                </g>
              ))}

            {description.locations.map((loc) => (
              <g key={loc.id} className={`mv-location mv-${loc.kind}`}>
                <title>{loc.label}</title>
                <LocationMarker kind={loc.kind} at={loc.position} />
                <Label
                  className="mv-label"
                  x={loc.position.x}
                  y={-loc.position.y + 14}
                  textAnchor="middle"
                >
                  {loc.label}
                </Label>
              </g>
            ))}

            {head && <Head description={description} at={head} />}
            {target && <TargetMarker at={target} />}
          </g>
        </Rotation.Provider>
      </svg>

      <div className="mv-overlay mv-overlay-top">
        <span className="mv-title">
          {description.name}
          {description.provisional && (
            <span className="chip chip-warn" title="Dimensions are estimates, not measurements">
              provisional
            </span>
          )}
        </span>
        <button type="button" className="icon-button" onClick={fit} title="Fit to view">
          <Maximize size={16} aria-hidden />
        </button>
      </div>
      {target && onMoveTo && (
        <MoveTargetBar
          target={target}
          reachable={inTravel(target)}
          canMove={canMove}
          onMove={() => {
            onMoveTo(target);
            setTarget(null);
          }}
          onCancel={() => setTarget(null)}
        />
      )}
      <div className="mv-overlay mv-overlay-bottom mono">
        {cursor ? `X ${cursor.x.toFixed(1)}  Y ${cursor.y.toFixed(1)}` : " "}
      </div>
    </div>
  );
}

function Grid({ description }: { description: MachineProfile }) {
  const { bed } = description;
  const lines = [];
  for (let x = Math.ceil(bed.x_min / GRID) * GRID; x <= bed.x_max; x += GRID) {
    lines.push(<line key={`x${x}`} x1={x} x2={x} y1={-bed.y_max} y2={-bed.y_min} />);
  }
  for (let y = Math.ceil(bed.y_min / GRID) * GRID; y <= bed.y_max; y += GRID) {
    lines.push(<line key={`y${y}`} x1={bed.x_min} x2={bed.x_max} y1={-y} y2={-y} />);
  }
  return <g className="mv-grid">{lines}</g>;
}

/** A staging plate: its outline, tapped holes, and camera knockout. */
function PlateShape({ plate }: { plate: StagingPlate }) {
  const { center, size } = plate;
  return (
    <g className="mv-plate">
      <title>{plate.label}</title>
      <rect
        x={center.x - size.x / 2}
        y={-center.y - size.y / 2}
        width={size.x}
        height={size.y}
        rx={3}
      />
      {plate.holes.map((h, i) => (
        <circle key={i} className="mv-plate-hole" cx={h.x} cy={-h.y} r={plate.hole_diameter / 2} />
      ))}
      {plate.corner_holes.map((c, i) => (
        <circle
          key={`c${i}`}
          className="mv-plate-hole"
          cx={center.x + c.x}
          cy={-(center.y + c.y)}
          r={plate.corner_hole_diameter / 2}
        />
      ))}
      {plate.knockout_diameter && (
        <circle
          className="mv-plate-knockout"
          cx={center.x}
          cy={-center.y}
          r={plate.knockout_diameter / 2}
        />
      )}
    </g>
  );
}

/** Feeders in one bank: the body extends behind the pick location, against the feed. */
function FeederBankShape({ bank }: { bank: FeederBank }) {
  const ahead = 8;
  const slots = Array.from({ length: bank.count }, (_, i) => ({
    x: bank.first_pick.x + bank.pitch.x * i,
    y: bank.first_pick.y + bank.pitch.y * i,
  }));
  return (
    <g className="mv-feeders">
      <title>{bank.label}</title>
      {slots.map((p, i) => (
        <g key={i} transform={`translate(${p.x} ${-p.y}) rotate(${-bank.rotation})`}>
          <rect
            className="mv-feeder"
            x={-(bank.length - ahead)}
            y={-bank.width / 2}
            width={bank.length}
            height={bank.width}
            rx={1.5}
          />
          <circle className="mv-pick" r={1.6} />
        </g>
      ))}
    </g>
  );
}

function LocationMarker({ kind, at }: { kind: string; at: Point }) {
  const x = at.x;
  const y = -at.y;
  switch (kind) {
    case "homing_fiducial":
    case "fiducial":
      return (
        <g>
          <circle cx={x} cy={y} r={5} className="mv-ring" />
          <circle cx={x} cy={y} r={1.8} className="mv-dot" />
        </g>
      );
    case "park":
      return (
        <g>
          <rect x={x - 7} y={y - 7} width={14} height={14} rx={3} />
          <Label className="mv-park-letter" x={x} y={y + 3.5} textAnchor="middle">
            P
          </Label>
        </g>
      );
    case "tip_holder":
      return (
        <g>
          <rect x={x - 15} y={y - 5} width={30} height={10} rx={2} />
          {[-10, 0, 10].map((dx) => (
            <circle key={dx} cx={x + dx} cy={y} r={2} className="mv-ring" />
          ))}
        </g>
      );
    default:
      return (
        <g>
          <rect x={x - 7} y={y - 7} width={14} height={14} rx={2} />
          <path d={`M${x - 4} ${y - 4} L${x + 4} ${y + 4} M${x + 4} ${y - 4} L${x - 4} ${y + 4}`} />
        </g>
      );
  }
}

function Head({ description, at }: { description: MachineProfile; at: Point }) {
  const camera = description.cameras.find((c) => c.mount === "head");
  const reach = 12;
  return (
    <g className="mv-head" transform={`translate(${at.x} ${-at.y})`}>
      {camera && (
        <rect
          className="mv-head-fov"
          x={camera.position.x - camera.field_of_view.x / 2}
          y={-camera.position.y - camera.field_of_view.y / 2}
          width={camera.field_of_view.x}
          height={camera.field_of_view.y}
        >
          <title>{camera.label} field of view</title>
        </rect>
      )}
      {description.nozzles.map((n) => (
        <g key={n.id}>
          <line className="mv-head-arm" x1={0} y1={0} x2={n.offset.x} y2={-n.offset.y} />
          <circle className="mv-nozzle" cx={n.offset.x} cy={-n.offset.y} r={3.5}>
            <title>{n.label}</title>
          </circle>
          <Label
            className="mv-label mv-label-head"
            x={n.offset.x}
            y={-n.offset.y + 11}
            textAnchor="middle"
          >
            {n.id}
          </Label>
        </g>
      ))}
      <path className="mv-crosshair" d={`M${-reach} 0 H${reach} M0 ${-reach} V${reach}`} />
      <circle className="mv-crosshair" r={2.5} />
    </g>
  );
}

function TargetMarker({ at }: { at: Point }) {
  const r = 6;
  return (
    <g className="mv-target" transform={`translate(${at.x} ${-at.y})`}>
      <circle r={r} />
      <path
        d={`M${-r - 4} 0 H${-r + 2} M${r - 2} 0 H${r + 4} M0 ${-r - 4} V${-r + 2} M0 ${r - 2} V${r + 4}`}
      />
    </g>
  );
}

function MoveTargetBar({
  target,
  reachable,
  canMove,
  onMove,
  onCancel,
}: {
  target: Point;
  reachable: boolean;
  canMove: boolean;
  onMove: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mv-move-bar" role="dialog" aria-label="Move to point">
      <span>
        Move camera to{" "}
        <span className="mono">
          X {target.x.toFixed(2)} Y {target.y.toFixed(2)}
        </span>
        {!reachable && <span className="text-danger"> · outside travel limits</span>}
      </span>
      <button
        type="button"
        className="button primary"
        disabled={!reachable || !canMove}
        onClick={onMove}
        title={canMove ? "Moves at safe Z" : "Motion is not available right now"}
      >
        Move
      </button>
      <button type="button" className="button" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}
