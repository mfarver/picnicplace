import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  ChevronsUp,
  RotateCcw,
  RotateCw,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import type { MachineProfile, MachineState } from "../api/types";
import { Panel } from "./Panel";

const LINEAR_STEPS = [0.1, 1, 10, 50];
const ROTARY_STEPS = [1, 5, 15, 90];

interface Props {
  description: MachineProfile;
  machine?: MachineState;
  /** Motion is allowed right now (connected, enabled, not busy). */
  canMove: boolean;
  onJog: (axis: string, distance: number) => void;
  /** Raise every nozzle to travel height. */
  onSafeZ: () => void;
}

/**
 * Step jogging. X and Y are disabled while a nozzle is below safe Z; the core refuses those
 * jogs anyway, and the safe-Z button is right there to fix it.
 */
export function JogPanel({ description, machine, canMove, onJog, onSafeZ }: Props) {
  const [step, setStep] = useState(1);
  const [turn, setTurn] = useState(15);

  const enabled = canMove && !!machine?.homed;
  const travelOk = enabled && !!machine?.z_safe;
  const travelHint = !machine?.homed
    ? "Home the machine first"
    : !machine.z_safe
      ? "Raise Z to safe height first"
      : undefined;

  const jogButton = (axis: string, sign: 1 | -1, label: string, icon: ReactNode) => (
    <button
      type="button"
      className="button jog-button"
      disabled={!travelOk}
      onClick={() => onJog(axis, sign * step)}
      title={travelHint ?? label}
      aria-label={label}
    >
      {icon}
    </button>
  );

  return (
    <Panel title="Jog">
      <StepPicker label="Step" unit="mm" steps={LINEAR_STEPS} value={step} onChange={setStep} />

      <div className="jog-layout">
        <div className="jog-pad">
          <span />
          {jogButton("Y", 1, "Y+ (back)", <ArrowUp size={16} />)}
          <span />
          {jogButton("X", -1, "X− (left)", <ArrowLeft size={16} />)}
          <button
            type="button"
            className="button jog-button"
            disabled={!enabled || machine?.z_safe}
            onClick={onSafeZ}
            title="Raise every nozzle to safe Z"
            aria-label="Raise to safe Z"
          >
            <ChevronsUp size={16} />
          </button>
          {jogButton("X", 1, "X+ (right)", <ArrowRight size={16} />)}
          <span />
          {jogButton("Y", -1, "Y− (front)", <ArrowDown size={16} />)}
          <span />
        </div>

        <div className="jog-nozzles">
          {description.nozzles.map((n) => {
            // Height on the nozzle's own scale: 0 at the top of its travel. With a shared Z
            // motor, "up" stops at the top rather than going on to lower the other nozzle.
            const height = Math.min(0, n.z_sign * (machine?.position[n.z_axis] ?? 0));
            const rise = Math.min(step, -height);
            return (
              <div key={n.id} className="jog-nozzle">
                <span className="jog-nozzle-label">{n.id}</span>
                <button
                  type="button"
                  className="button jog-button-sm"
                  disabled={!enabled || rise <= 0}
                  onClick={() => onJog(n.z_axis, n.z_sign * rise)}
                  title={`${n.label} up`}
                  aria-label={`${n.id} up`}
                >
                  <ArrowUp size={14} />
                </button>
                <button
                  type="button"
                  className="button jog-button-sm"
                  disabled={!enabled}
                  onClick={() => onJog(n.z_axis, -n.z_sign * step)}
                  title={`${n.label} down`}
                  aria-label={`${n.id} down`}
                >
                  <ArrowDown size={14} />
                </button>
                <button
                  type="button"
                  className="button jog-button-sm"
                  disabled={!enabled}
                  onClick={() => onJog(n.rotation_axis, -turn)}
                  title={`${n.label}: rotate ${turn}° clockwise`}
                  aria-label={`${n.id} rotate clockwise`}
                >
                  <RotateCw size={14} />
                </button>
                <button
                  type="button"
                  className="button jog-button-sm"
                  disabled={!enabled}
                  onClick={() => onJog(n.rotation_axis, turn)}
                  title={`${n.label}: rotate ${turn}° counterclockwise`}
                  aria-label={`${n.id} rotate counterclockwise`}
                >
                  <RotateCcw size={14} />
                </button>
              </div>
            );
          })}
        </div>
      </div>

      <StepPicker label="Turn" unit="°" steps={ROTARY_STEPS} value={turn} onChange={setTurn} />
      {enabled && travelHint && <p className="note text-muted">{travelHint} to jog X or Y.</p>}
    </Panel>
  );
}

function StepPicker({
  label,
  unit,
  steps,
  value,
  onChange,
}: {
  label: string;
  unit: string;
  steps: number[];
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="step-picker" role="radiogroup" aria-label={`${label} size`}>
      <span className="step-picker-label">{label}</span>
      {steps.map((s) => (
        <button
          key={s}
          type="button"
          role="radio"
          aria-checked={s === value}
          className="step-option"
          onClick={() => onChange(s)}
        >
          {s}
          {unit === "°" ? "°" : ""}
        </button>
      ))}
      {unit !== "°" && <span className="text-muted">{unit}</span>}
    </div>
  );
}
