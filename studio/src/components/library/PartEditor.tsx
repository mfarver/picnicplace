import { RotateCcw, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Footprint, Part, Tape } from "../../api/types";
import { TAPE_WIDTHS, standardTape } from "../../tape";
import { Field, NumberInput } from "../Form";
import { FootprintDrawing } from "./FootprintShape";
import { TapeDrawing } from "./TapeDrawing";
import { BasisSelect, RotationHelp } from "./TapeEditor";

interface Props {
  part: Part;
  parts: Part[];
  footprints: Footprint[];
  tapes: Tape[];
  busy: boolean;
  onSave: (part: Part) => void;
  onDelete: () => void;
  onOpenTape: (id: string) => void;
  /** Save a tape (made from values here), resolving to it once saved. */
  onCreateTape: (tape: Tape) => Promise<Tape | undefined>;
}

/**
 * View and edit one part: its footprint, height, and how it sits in its tape. Give it a
 * key per part, so switching parts starts a fresh draft.
 */
export function PartEditor({
  part,
  parts,
  footprints,
  tapes,
  busy,
  onSave,
  onDelete,
  onOpenTape,
  onCreateTape,
}: Props) {
  const [draft, setDraft] = useState<Part>(part);
  const footprint = footprints.find((f) => f.id === draft.footprint_id);
  const changed = JSON.stringify(draft) !== JSON.stringify(part);
  const set = (update: Partial<Part>) => setDraft((d) => ({ ...d, ...update }));
  const tape = tapes.find((t) => t.id === draft.tape_id);
  const others = tape ? parts.filter((p) => p.tape_id === tape.id && p.id !== part.id).length : 0;
  // Tapes other parts with this footprint come in: the likely ones.
  const sameShape = tapes.filter((t) =>
    parts.some((p) => p.tape_id === t.id && p.footprint_id === draft.footprint_id),
  );

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
          <Field label="Comes in">
            <select
              className="input"
              value={draft.tape_id ?? ""}
              onChange={(e) => set({ tape_id: e.target.value || null })}
            >
              <option value="">Not known</option>
              {sameShape.length > 0 && (
                <optgroup label={`Used with ${draft.footprint_id}`}>
                  {sameShape.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name || t.id}
                    </option>
                  ))}
                </optgroup>
              )}
              <optgroup label="All tapes">
                {tapes
                  .filter((t) => !sameShape.includes(t))
                  .map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name || t.id}
                    </option>
                  ))}
              </optgroup>
            </select>
          </Field>
          {tape ? (
            <>
              <div className="drawing-box">
                <TapeDrawing tape={tape} footprint={footprint} />
              </div>
              <p className="text-muted note">
                {others > 0
                  ? `${others} other part${others === 1 ? "" : "s"} come in this tape too.`
                  : "No other part comes in this tape yet."}{" "}
                <button type="button" className="link-button" onClick={() => onOpenTape(tape.id)}>
                  Edit the tape
                </button>
              </p>
            </>
          ) : (
            <NewTape
              busy={busy}
              onCreate={async (t) => {
                const created = await onCreateTape(t);
                if (created) set({ tape_id: created.id });
              }}
            />
          )}
        </section>
      </div>
    </div>
  );
}

/** Make a tape from its values (or find the one with the same values) for a part. */
function NewTape({ busy, onCreate }: { busy: boolean; onCreate: (tape: Tape) => void }) {
  const [kind, setKind] = useState<Tape["kind"]>("paper");
  const [width, setWidth] = useState<number | null>(8);
  const [pitch, setPitch] = useState<number | null>(4);
  const [rotation, setRotation] = useState<number | null>(0);
  return (
    <>
      <p className="text-muted note">
        Not in a tape the library knows yet. Make one from its values; a tape with the same
        values is shared rather than made again.
      </p>
      <div className="field-row">
        <Field label="Kind">
          <select
            className="input"
            value={kind}
            onChange={(e) => setKind(e.target.value as Tape["kind"])}
          >
            <option value="paper">Paper</option>
            <option value="embossed">Embossed</option>
            <option value="unknown">Unknown</option>
          </select>
        </Field>
        <Field label="Width, mm">
          <select
            className="input"
            value={width ?? ""}
            onChange={(e) => setWidth(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">Not known</option>
            {TAPE_WIDTHS.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Pitch, mm">
          <NumberInput value={pitch} onChange={setPitch} min={0} />
        </Field>
        <Field label="Rotation, °">
          <NumberInput value={rotation} onChange={setRotation} />
        </Field>
      </div>
      <RotationHelp />
      <button
        type="button"
        className="button"
        disabled={busy}
        onClick={() => onCreate(standardTape(kind, width, pitch, rotation))}
      >
        Use this tape
      </button>
    </>
  );
}
