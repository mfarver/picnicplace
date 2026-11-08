import { RotateCcw, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Basis, Footprint, Part, Tape } from "../../api/types";
import { Field, NumberInput } from "../Form";
import { FootprintDrawing } from "./FootprintShape";
import { TapeDrawing } from "./TapeDrawing";

const BASES: Basis[] = ["datasheet", "standard", "observed", "measured", "estimated", "unknown"];
const TAPE_WIDTHS = [8, 12, 16, 24, 32, 44, 56];
const QUADRANTS = ["Q1", "Q2", "Q3", "Q4"] as const;
const NEW_TAPE: Tape = {
  kind: "unknown",
  width: 8,
  pitch: 4,
  rotation: 0,
  pin1_quadrant: null,
  basis: "unknown",
  source: "",
};

interface Props {
  part: Part;
  footprints: Footprint[];
  busy: boolean;
  onSave: (part: Part) => void;
  onDelete: () => void;
}

/**
 * View and edit one part: its footprint, height, and how it sits in its tape. Give it a
 * key per part, so switching parts starts a fresh draft.
 */
export function PartEditor({ part, footprints, busy, onSave, onDelete }: Props) {
  const [draft, setDraft] = useState<Part>(part);
  const footprint = footprints.find((f) => f.id === draft.footprint_id);
  const changed = JSON.stringify(draft) !== JSON.stringify(part);
  const set = (update: Partial<Part>) => setDraft((d) => ({ ...d, ...update }));
  const setTape = (update: Partial<Tape>) =>
    setDraft((d) => ({ ...d, tape: { ...(d.tape ?? NEW_TAPE), ...update } }));

  return (
    <div className="editor">
      <header className="editor-header">
        <div>
          <h2 className="editor-title">{part.id}</h2>
          <p className="text-muted">{[part.name, part.manufacturer].filter(Boolean).join(" · ")}</p>
        </div>
        <div className="button-row">
          <button
            type="button"
            className="button"
            disabled={!changed || busy}
            onClick={() => setDraft(part)}
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
            disabled={busy}
            onClick={onDelete}
            title="Delete part"
          >
            <Trash2 size={16} aria-label="Delete part" />
          </button>
        </div>
      </header>

      <div className="editor-grid">
        <section className="editor-section">
          <h3 className="section-title">Part</h3>
          <Field label="Name or value">
            <input
              className="input"
              value={draft.name}
              onChange={(e) => set({ name: e.target.value })}
            />
          </Field>
          <Field label="Manufacturer">
            <input
              className="input"
              value={draft.manufacturer}
              onChange={(e) => set({ manufacturer: e.target.value })}
            />
          </Field>
          <Field label="Footprint">
            <select
              className="input"
              value={draft.footprint_id}
              onChange={(e) => set({ footprint_id: e.target.value })}
            >
              {footprints.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.id}
                </option>
              ))}
            </select>
          </Field>
          <div className="field-row">
            <Field label="Height, mm">
              <NumberInput value={draft.height} onChange={(height) => set({ height })} min={0} />
            </Field>
            <Field label="From">
              <BasisSelect
                value={draft.height_basis}
                onChange={(height_basis) => set({ height_basis })}
              />
            </Field>
          </div>
          <Field label="Height source">
            <input
              className="input"
              value={draft.height_source}
              placeholder="Datasheet, page, or link"
              onChange={(e) => set({ height_source: e.target.value })}
            />
          </Field>
          {footprint && (
            <div className="drawing-box">
              <FootprintDrawing footprint={footprint} />
              <p className="caption">
                {footprint.id}: {footprint.pads.length} pads
                {footprint.body ? `, body ${footprint.body.x} × ${footprint.body.y} mm` : ""}
              </p>
            </div>
          )}
        </section>

        <section className="editor-section">
          <h3 className="section-title">Tape</h3>
          {draft.tape ? (
            <>
              <div className="field-row">
                <Field label="Kind">
                  <select
                    className="input"
                    value={draft.tape.kind}
                    onChange={(e) => setTape({ kind: e.target.value as Tape["kind"] })}
                  >
                    <option value="paper">Paper</option>
                    <option value="embossed">Embossed</option>
                    <option value="unknown">Unknown</option>
                  </select>
                </Field>
                <Field label="Width, mm">
                  <select
                    className="input"
                    value={draft.tape.width ?? ""}
                    onChange={(e) =>
                      setTape({ width: e.target.value ? Number(e.target.value) : null })
                    }
                  >
                    <option value="">Unknown</option>
                    {TAPE_WIDTHS.map((w) => (
                      <option key={w} value={w}>
                        {w}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Pitch, mm">
                  <NumberInput
                    value={draft.tape.pitch}
                    onChange={(pitch) => setTape({ pitch })}
                    min={0}
                  />
                </Field>
              </div>
              <Field label="Rotation in tape">
                <div className="segmented">
                  {[0, 90, 180, -90].map((angle) => (
                    <button
                      key={angle}
                      type="button"
                      className={draft.tape?.rotation === angle ? "segment active" : "segment"}
                      onClick={() => setTape({ rotation: angle })}
                    >
                      {angle}°
                    </button>
                  ))}
                  <NumberInput
                    value={draft.tape.rotation}
                    onChange={(rotation) => setTape({ rotation: rotation ?? 0 })}
                  />
                </div>
              </Field>
              <div className="field-row">
                <Field label="Pin 1 quadrant">
                  <select
                    className="input"
                    value={draft.tape.pin1_quadrant ?? ""}
                    onChange={(e) =>
                      setTape({
                        pin1_quadrant: (e.target.value || null) as Tape["pin1_quadrant"],
                      })
                    }
                  >
                    <option value="">Not given</option>
                    {QUADRANTS.map((q) => (
                      <option key={q} value={q}>
                        {q}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="From">
                  <BasisSelect value={draft.tape.basis} onChange={(basis) => setTape({ basis })} />
                </Field>
              </div>
              <Field label="Source">
                <input
                  className="input"
                  value={draft.tape.source}
                  placeholder="Tape and reel drawing, or what the reel shows"
                  onChange={(e) => setTape({ source: e.target.value })}
                />
              </Field>
              <div className="drawing-box">
                <TapeDrawing tape={draft.tape} footprint={footprint} />
                <p className="caption">
                  Looking down on the cover tape, sprocket holes at the top, feeding left to right.
                  Pin 1 is the highlighted pad
                  {draft.tape.pin1_quadrant ? "; the dot is the datasheet's quadrant" : ""}.
                </p>
              </div>
              <button
                type="button"
                className="button button-sm"
                onClick={() => set({ tape: null })}
              >
                Remove tape details
              </button>
            </>
          ) : (
            <>
              <p className="text-muted">No tape details yet.</p>
              <button type="button" className="button" onClick={() => set({ tape: NEW_TAPE })}>
                Add tape details
              </button>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function BasisSelect({ value, onChange }: { value: Basis; onChange: (b: Basis) => void }) {
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
