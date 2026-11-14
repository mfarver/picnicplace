import { ChevronDown, ChevronUp } from "lucide-react";
import { useState } from "react";
import type {
  MachineInput,
  MachineOutput,
  MachineProfile,
  MachineState,
  Nozzle,
} from "../../api/types";

/**
 * Side view of the head, floating over the machine view (spec section 8.3): nozzle heights
 * against safe Z, rotation dials, vacuum bars, input lights, and output toggles. Everything
 * is generated from the machine profile.
 */

interface Props {
  description: MachineProfile;
  machine?: MachineState;
  /** Outputs can be switched on (motion is enabled). */
  canActuate: boolean;
  busy: boolean;
  /** Another client has control: outputs are shown but cannot be switched. */
  readOnly?: boolean;
  onSetOutput: (id: string, on: boolean) => void;
}

/** Height of a nozzle on its Z axis; zero is the top of travel (matches the core). */
function nozzleHeight(nozzle: Nozzle, machine?: MachineState): number {
  const z = machine?.position[nozzle.z_axis] ?? 0;
  return Math.min(0, nozzle.z_sign * z);
}

function partHeld(nozzle: Nozzle, d: MachineProfile, machine?: MachineState): boolean {
  if (!nozzle.vacuum_input) return false;
  const input = d.inputs.find((i) => i.id === nozzle.vacuum_input);
  const value = machine?.inputs[nozzle.vacuum_input];
  return (
    typeof value === "number" && !!input && pastThreshold(input, value, threshold(input, d))
  );
}

/** Where a value falls from range.min (0) to range.max (1). The range may run backward. */
function rangeFraction(input: MachineInput, value: number): number {
  const { min, max } = input.range ?? { min: 0, max: 100 };
  return (value - min) / (max - min);
}

/** A vacuum sensor's pick threshold: its nozzle's measured one, else the sensor's. */
function threshold(input: MachineInput, d: MachineProfile): number | null {
  const nozzle = d.nozzles.find((n) => n.vacuum_input === input.id);
  return nozzle?.vacuum?.threshold ?? input.threshold ?? null;
}

/** A vacuum reading at or past the pick threshold, going toward range.max (matches the core). */
function pastThreshold(input: MachineInput, value: number, at: number | null): boolean {
  if (at == null || !input.range) return false;
  return rangeFraction(input, value) >= rangeFraction(input, at);
}

/** Large raw readings (sensor counts) read better as -1.50M than -1500000.0. */
function formatReading(value: number, input: MachineInput): string {
  const span = input.range ? Math.abs(input.range.max - input.range.min) : 100;
  if (span >= 1e5) return `${(value / 1e6).toFixed(2)}M`;
  if (span >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
  return value.toFixed(1);
}

export function HeadInset({
  description,
  machine,
  canActuate,
  busy,
  readOnly = false,
  onSetOutput,
}: Props) {
  const [open, setOpen] = useState(true);

  const vacuumInputs = description.inputs.filter((i) => i.role === "vacuum");
  const lights = description.inputs.filter(
    (i) => i.kind === "digital" && i.id !== description.safe_z_input,
  );
  const safeInput = description.inputs.find((i) => i.id === description.safe_z_input);

  return (
    <section className={`head-inset${open ? "" : " head-inset-collapsed"}`} aria-label="Head">
      <header className="head-inset-header">
        <span className="panel-title-text">Head</span>
        <button
          type="button"
          className="icon-button icon-button-sm"
          onClick={() => setOpen(!open)}
          title={open ? "Collapse" : "Expand"}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={14} aria-hidden /> : <ChevronUp size={14} aria-hidden />}
        </button>
      </header>

      {open && (
        <div className="head-inset-body">
          <SideView description={description} machine={machine} />

          {vacuumInputs.length > 0 && (
            <div className="hi-group">
              {vacuumInputs.map((input) => (
                <VacuumBar
                  key={input.id}
                  input={input}
                  value={machine?.inputs[input.id]}
                  threshold={threshold(input, description)}
                  measured={description.nozzles.some(
                    (n) => n.vacuum_input === input.id && n.vacuum != null,
                  )}
                />
              ))}
            </div>
          )}

          <div className="hi-group hi-lights">
            <Light
              label={safeInput ? safeInput.label : "Safe Z"}
              on={machine?.z_safe ?? false}
              sensed={!!safeInput}
              title={
                safeInput
                  ? "Safe Z, read from a sensor"
                  : "Safe Z, computed from the Z position (no sensor)"
              }
            />
            {lights.map((input) => (
              <Light
                key={input.id}
                label={input.label}
                on={machine?.inputs[input.id] === true}
                sensed
              />
            ))}
          </div>

          {description.outputs.length > 0 && (
            <div className="hi-group hi-outputs">
              {description.outputs.map((output) => (
                <OutputToggle
                  key={output.id}
                  output={output}
                  on={machine?.outputs[output.id] ?? false}
                  disabled={!machine?.connected || busy || readOnly}
                  canTurnOn={canActuate}
                  onSet={onSetOutput}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// --- Side view ---------------------------------------------------------------------------

const COLUMN = 64;
const TOP = 30;
const DEPTH_PX = 70;
/** Side view pixels per mm across a tip: its body (about 6 mm) is drawn 8 px wide. */
const TIP_PX_PER_MM = 1.3;

/** Half the drawn width of a nozzle's end: its fitted tip's, if known, else a generic one. */
function tipHalfWidth(nozzle: Nozzle, d: MachineProfile): number {
  const tip = d.nozzle_tips.find((t) => t.id === nozzle.tip);
  if (!tip?.outer_diameter) return 1.5;
  return Math.min(7, Math.max(0.75, (tip.outer_diameter / 2) * TIP_PX_PER_MM));
}

function SideView({
  description,
  machine,
}: {
  description: MachineProfile;
  machine?: MachineState;
}) {
  const nozzles = description.nozzles;
  // Scale: the deepest a nozzle can go on its Z axis.
  const depth = Math.max(
    1,
    ...nozzles.map((n) => {
      const axis = description.axes.find((a) => a.name === n.z_axis);
      return axis ? Math.max(-axis.min, axis.max) : 1;
    }),
  );
  const toY = (height: number) => TOP + (-height / depth) * DEPTH_PX;
  const width = Math.max(1, nozzles.length) * COLUMN + 16;
  const height = TOP + DEPTH_PX + 22;
  const safeY = toY(description.safe_z);

  return (
    <svg className="hi-side" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Nozzles">
      <rect className="hi-carriage" x={4} y={4} width={width - 8} height={TOP - 10} rx={4} />
      <line className="hi-safe" x1={2} x2={width - 2} y1={safeY} y2={safeY}>
        <title>Safe Z</title>
      </line>
      <text className="hi-safe-label" x={width - 4} y={safeY - 3} textAnchor="end">
        safe Z
      </text>
      {nozzles.map((n, i) => {
        const cx = 8 + COLUMN * i + COLUMN / 2;
        const h = nozzleHeight(n, machine);
        const tipY = toY(h);
        const held = partHeld(n, description, machine);
        const rotation = machine?.position[n.rotation_axis] ?? 0;
        const low = h < description.safe_z;
        const end = tipHalfWidth(n, description);
        return (
          <g key={n.id}>
            <title>
              {`${n.label}${n.tip ? ` (${n.tip} tip)` : ""}: ${h.toFixed(2)} mm, ` +
                `${rotation.toFixed(1)}°${held ? ", part held" : ""}`}
            </title>
            <Dial cx={cx} cy={TOP / 2 - 1} angle={rotation} label={n.id} />
            <rect
              className={`hi-shaft${low ? " hi-low" : ""}`}
              x={cx - 2.5}
              y={TOP - 6}
              width={5}
              height={Math.max(0, tipY - (TOP - 6) - 5)}
            />
            <path
              className={`hi-tip${low ? " hi-low" : ""}`}
              d={
                `M${cx - 4} ${tipY - 6} L${cx + 4} ${tipY - 6} ` +
                `L${cx + end} ${tipY} L${cx - end} ${tipY} Z`
              }
            />
            {held && (
              <rect className="hi-part" x={cx - 6} y={tipY + 1} width={12} height={4} rx={1} />
            )}
            <text className="hi-value" x={cx} y={height - 4} textAnchor="middle">
              {h.toFixed(1)} mm
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Dial({ cx, cy, angle, label }: { cx: number; cy: number; angle: number; label: string }) {
  const r = 9;
  const rad = ((angle - 90) * Math.PI) / 180;
  return (
    <g className="hi-dial">
      <circle cx={cx} cy={cy} r={r} />
      <line
        x1={cx}
        y1={cy}
        x2={cx + Math.cos(rad) * (r - 1.5)}
        y2={cy + Math.sin(rad) * (r - 1.5)}
      />
      <text x={cx - r - 4} y={cy + 3} textAnchor="end">
        {label}
      </text>
      <text x={cx + r + 4} y={cy + 3} className="hi-dial-angle">
        {`${angle.toFixed(0)}°`}
      </text>
    </g>
  );
}

// --- Vacuum, lights, outputs -------------------------------------------------------------

function VacuumBar({
  input,
  value,
  threshold,
  measured,
}: {
  input: MachineInput;
  value: number | boolean | undefined;
  threshold: number | null;
  measured: boolean;
}) {
  const { min, max } = input.range ?? { min: 0, max: 100 };
  const reading = typeof value === "number" ? value : 0;
  const frac = (v: number) => Math.min(1, Math.max(0, rangeFraction(input, v)));
  const held = pastThreshold(input, reading, threshold);
  return (
    <div className={`hi-vacuum${held ? " hi-lit" : ""}`}>
      <span className="hi-vacuum-label">{input.label}</span>
      <span
        className="hi-bar"
        role="meter"
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={reading}
        aria-label={input.label}
      >
        <span className="hi-bar-fill" style={{ width: `${frac(reading) * 100}%` }} />
        {threshold != null && (
          <span
            className="hi-bar-threshold"
            style={{ left: `${frac(threshold) * 100}%` }}
            title={
              `Pick threshold ${formatReading(threshold, input)} ${input.unit ?? ""}`.trimEnd() +
              (measured ? ", measured for this nozzle" : ", from the profile")
            }
          />
        )}
      </span>
      <span className="hi-vacuum-value mono">
        {formatReading(reading, input)}
        {input.unit && input.unit !== "counts" && <span className="text-muted"> {input.unit}</span>}
      </span>
    </div>
  );
}

function Light({
  label,
  on,
  sensed,
  title,
}: {
  label: string;
  on: boolean;
  sensed: boolean;
  title?: string;
}) {
  return (
    <span className="hi-light" title={title ?? label}>
      <span
        className={`hi-lamp${on ? " hi-lamp-on" : ""}${sensed ? "" : " hi-lamp-computed"}`}
        aria-hidden
      />
      {label}
      <span className="visually-hidden">{on ? " on" : " off"}</span>
    </span>
  );
}

function OutputToggle({
  output,
  on,
  disabled,
  canTurnOn,
  onSet,
}: {
  output: MachineOutput;
  on: boolean;
  disabled: boolean;
  canTurnOn: boolean;
  onSet: (id: string, on: boolean) => void;
}) {
  const blocked = disabled || (!on && !canTurnOn);
  return (
    <button
      type="button"
      className={`hi-output${on ? " hi-output-on" : ""}`}
      aria-pressed={on}
      disabled={blocked}
      onClick={() => onSet(output.id, !on)}
      title={blocked && !on ? "Motion must be enabled to switch outputs on" : output.label}
    >
      <span className="hi-output-dot" aria-hidden />
      {output.label}
    </button>
  );
}
