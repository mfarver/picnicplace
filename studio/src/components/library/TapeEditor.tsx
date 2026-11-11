import { RotateCcw, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Basis, Footprint, Part, Tape } from "../../api/types";
import {
  QUADRANT_PLACE,
  QUADRANT_ROTATION,
  TAPE_WIDTHS,
  formatRotation,
  parseLength,
  parseRotation,
  pin1Quadrant,
  type Parsed,
  type Quadrant,
} from "../../tape";
import { Field, NumberInput } from "../Form";
import { TapeDrawing } from "./TapeDrawing";

const BASES: Basis[] = [
  "datasheet",
  "standard",
  "observed",
  "measured",
  "estimated",
  "unknown",
];
const QUADRANTS: Quadrant[] = ["Q1", "Q2", "Q3", "Q4"];
const WIDTH_TIP = "Tape width (EIA-481 W) in mm, or with a unit; empty if not known";
const PITCH_TIP =
  "From one part to the next (EIA-481 P1) in mm, or with a unit; empty if not known";
const ROTATION_TIP =
  "The part's turn in its pocket, degrees counterclockwise, seen with the sprocket holes " +
  "at the top and the tape feeding left to right; empty if not known";

export function BasisSelect({ value, onChange }: { value: Basis; onChange: (b: Basis) => void }) {
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value as Basis)}>
      {BASES.map((b) => (
        <option key={b} value={b}>
          {b[0]!.toUpperCase() + b.slice(1)}
        </option>
      ))}
    </select>
  );
}

/** How to read the rotation in tape from a reel or a datasheet (the help text). */
export function RotationHelp() {
  return (
    <details className="tape-help">
      <summary>How to read the rotation in tape</summary>
      <ol>
        <li>
          Hold the tape, or look at its drawing, with the sprocket holes along the top and the
          tape feeding left to right.
        </li>
        <li>
          Compare the part in its pocket with its footprint at 0°. Turned counterclockwise is
          positive.
        </li>
        <li>
          For a part with pin 1 at the top left at 0°, the datasheet's pin-1 quadrant gives it:
          Q1 (top left) 0°, Q2 (top right) −90°, Q3 (bottom left) +90°, Q4 (bottom right)
          180°. The quadrant buttons do this for you.
        </li>
        <li>
          For pin 1 on the center line, such as a diode's cathode, the quadrant does not settle
          it: look at which way pin 1 (the band) points.
        </li>
        <li>
          A part the same both ways round, such as a resistor or a ceramic capacitor, places
          the same at +90° and −90°, and at 0° and 180°.
        </li>
      </ol>
    </details>
  );
}

/** A pocket seen as in the tape drawing, sprocket holes along the top, with a quadrant
 * shaded: where Q1 to Q4 are, for anyone new to them. */
function QuadrantIcon({ quadrant }: { quadrant: Quadrant }) {
  const left = quadrant === "Q1" || quadrant === "Q3";
  const top = quadrant === "Q1" || quadrant === "Q2";
  return (
    <svg className="quadrant-icon" viewBox="0 0 14 14" aria-hidden="true">
      <rect className="quadrant-pin1" x={left ? 1 : 7} y={top ? 1 : 7} width={6} height={6} />
      <rect className="quadrant-pocket" x={1} y={1} width={12} height={12} />
      <path className="quadrant-pocket" d="M7 1V13M1 7H13" />
    </svg>
  );
}

/** The rotation that puts the footprint's pin 1 in a quadrant, or the datasheet table's. */
function rotationFor(q: Quadrant, footprint?: Footprint): number {
  if (footprint) {
    for (const r of [0, 90, 180, -90]) if (pin1Quadrant(footprint, r) === q) return r;
  }
  return QUADRANT_ROTATION[q];
}

interface Props {
  tape: Tape;
  parts: Part[];
  footprints: Footprint[];
  busy: boolean;
  onSave: (tape: Tape) => void;
  onDelete: () => void;
  onOpenPart: (id: string) => void;
}

/**
 * View and edit one tape, which every part on it shares. Give it a key per tape, so
 * switching tapes starts a fresh draft.
 */
export function TapeEditor({ tape, parts, footprints, busy, onSave, onDelete, onOpenPart }: Props) {
  const [draft, setDraft] = useState<Tape>(tape);
  const users = parts.filter((p) => p.tape_id === tape.id);
  const shapes = [...new Set(users.map((p) => p.footprint_id))];
  const [shown, setShown] = useState<string>(shapes[0] ?? footprints[0]?.id ?? "");
  const footprint = footprints.find((f) => f.id === shown);
  const come = users.length === 1 ? "1 part comes" : `${users.length} parts come`;
  const changed = JSON.stringify(draft) !== JSON.stringify(tape);
  const set = (update: Partial<Tape>) => setDraft((d) => ({ ...d, ...update }));
  const widths = draft.width !== null && !TAPE_WIDTHS.includes(draft.width)
    ? [...TAPE_WIDTHS, draft.width]
    : TAPE_WIDTHS;

  return (
    <div className="editor">
      <header className="editor-header">
        <div>
          <h2 className="editor-title">{tape.name || tape.id}</h2>
          <p className="text-muted">
            Tape {tape.id}
            {users.length
              ? ` · ${come} in it: ` +
                "a change here changes them all"
              : " · no parts come in it yet"}
          </p>
        </div>
        <div className="button-row">
          <button
            type="button"
            className="button"
            disabled={!changed || busy}
            onClick={() => setDraft(tape)}
          >
            <RotateCcw size={16} aria-hidden />
            Revert
          </button>
          <button
            type="button"
            className="button primary"
            disabled={!changed || busy}
            onClick={() => onSave(draft)}
          >
            <Save size={16} aria-hidden />
            Save
          </button>
          <button
            type="button"
            className="button button-icon"
            disabled={busy || users.length > 0}
            onClick={onDelete}
            title={users.length ? "Parts come in this tape" : "Delete tape"}
          >
            <Trash2 size={16} aria-label="Delete tape" />
          </button>
        </div>
      </header>

      <div className="editor-grid">
        <section className="editor-section">
          <h3 className="section-title">Tape</h3>
          <Field label="Name">
            <input
              className="input"
              value={draft.name}
              onChange={(e) => set({ name: e.target.value })}
            />
          </Field>
          <div className="field-row">
            <Field label="Kind">
              <select
                className="input"
                value={draft.kind}
                onChange={(e) => set({ kind: e.target.value as Tape["kind"] })}
              >
                <option value="paper">Paper</option>
                <option value="embossed">Embossed</option>
                <option value="unknown">Unknown</option>
              </select>
            </Field>
            <Field label="Width (W), mm">
              <select
                className="input"
                value={draft.width ?? ""}
                onChange={(e) => set({ width: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">Not known</option>
                {widths.map((w) => (
                  <option key={w} value={w}>
                    {w}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Pitch (P1), mm">
              <NumberInput value={draft.pitch} onChange={(pitch) => set({ pitch })} min={0} />
            </Field>
          </div>
          <Field label="Rotation in tape">
            <div className="segmented">
              {[0, 90, 180, -90].map((angle) => (
                <button
                  key={angle}
                  type="button"
                  className={draft.rotation === angle ? "segment active" : "segment"}
                  onClick={() => set({ rotation: angle })}
                >
                  {angle}°
                </button>
              ))}
              <NumberInput value={draft.rotation} onChange={(rotation) => set({ rotation })} />
            </div>
          </Field>
          <Field label="Or the datasheet's pin-1 quadrant">
            <div className="segmented">
              {QUADRANTS.map((q) => (
                <button
                  key={q}
                  type="button"
                  className={
                    footprint &&
                    draft.rotation !== null &&
                    pin1Quadrant(footprint, draft.rotation) === q
                      ? "segment active"
                      : "segment"
                  }
                  title={`Turn the part so pin 1 is in ${q}, the pocket's ${QUADRANT_PLACE[q]}`}
                  onClick={() => set({ rotation: rotationFor(q, footprint) })}
                >
                  <QuadrantIcon quadrant={q} />
                  {q}
                </button>
              ))}
            </div>
          </Field>
          <RotationHelp />
          <div className="field-row">
            <Field label="From">
              <BasisSelect value={draft.basis} onChange={(basis) => set({ basis })} />
            </Field>
            <Field label="Source">
              <input
                className="input"
                value={draft.source}
                placeholder="Tape and reel drawing, or the reel"
                onChange={(e) => set({ source: e.target.value })}
              />
            </Field>
          </div>
          <Field label="Notes">
            <input
              className="input"
              value={draft.notes}
              placeholder="Such as: 8 mm paper, black pockets"
              onChange={(e) => set({ notes: e.target.value })}
            />
          </Field>
        </section>

        <section className="editor-section">
          <h3 className="section-title">Preview</h3>
          <Field label="Footprint">
            <select className="input" value={shown} onChange={(e) => setShown(e.target.value)}>
              {shapes.length > 0 && (
                <optgroup label="Of the parts on this tape">
                  {shapes.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </optgroup>
              )}
              <optgroup label="All footprints">
                {footprints
                  .filter((f) => !shapes.includes(f.id))
                  .map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.id}
                    </option>
                  ))}
              </optgroup>
            </select>
          </Field>
          <div className="drawing-box">
            <TapeDrawing tape={draft} footprint={footprint} />
          </div>
          {users.length > 0 && (
            <>
              <h3 className="section-title">Parts on this tape</h3>
              <ul className="chip-list">
                {users.map((p) => (
                  <li key={p.id}>
                    <button type="button" className="chip" onClick={() => onOpenPart(p.id)}>
                      {p.id}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

/**
 * Every tape in a table whose width, pitch, and rotation can be edited in place: the
 * quickest way to check many. An empty cell clears a value (not known); a length can carry
 * a unit (mm if not); text that is not a number is refused and the old value kept.
 */
export function TapeTable({
  tapes,
  parts,
  selected,
  busy,
  onSelect,
  onSave,
}: {
  tapes: Tape[];
  parts: Part[];
  selected: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onSave: (tape: Tape) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const count = (id: string) => parts.filter((p) => p.tape_id === id).length;
  const cell = (
    tape: Tape,
    key: "width" | "pitch" | "rotation",
    parse: (text: string) => Parsed,
    format: (v: number | null) => string,
    title: string,
  ) => (
    <td>
      <input
        key={`${tape.id}:${String(tape[key])}`}
        className="input input-cell"
        defaultValue={format(tape[key])}
        title={title}
        disabled={busy}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
        onBlur={(e) => {
          const result = parse(e.currentTarget.value);
          if (!result.ok) {
            setError(result.error);
            e.currentTarget.value = format(tape[key]);
            return;
          }
          setError(null);
          if (result.value !== tape[key]) onSave({ ...tape, [key]: result.value });
          else e.currentTarget.value = format(tape[key]);
        }}
      />
    </td>
  );
  const length = (v: number | null) => (v === null ? "" : String(v));
  return (
    <div className="tape-table-box">
      <table className="tape-table">
        <thead>
          <tr>
            <th>Tape</th>
            <th title={WIDTH_TIP}>W</th>
            <th title={PITCH_TIP}>P1</th>
            <th title={ROTATION_TIP}>
              Rotation
            </th>
            <th>Parts</th>
          </tr>
        </thead>
        <tbody>
          {tapes.map((t) => (
            <tr
              key={t.id}
              className={t.id === selected ? "active" : undefined}
              onClick={() => onSelect(t.id)}
            >
              <td>
                <span className="item-id">{t.name || t.id}</span>
              </td>
              {cell(t, "width", parseLength, length, WIDTH_TIP)}
              {cell(t, "pitch", parseLength, length, PITCH_TIP)}
              {cell(t, "rotation", parseRotation, formatRotation, ROTATION_TIP)}
              <td className="text-muted">{count(t.id)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <p className="note note-danger">{error}: kept the old value.</p>}
    </div>
  );
}
