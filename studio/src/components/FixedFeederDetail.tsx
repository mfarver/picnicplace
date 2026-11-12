import { Crosshair, MoveRight, RefreshCw, ScanSearch, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useAction } from "../api/useAction";
import type { FeedersState, FixedFeeder, FixedFeederState, Part, Point, Tape } from "../api/types";
import { Field, NumberInput } from "./Form";
import { LearnedPicks } from "./PickCorrection";
import { WidthSelect, WidthWarning } from "./TapeWidth";
import { remainingText } from "./machine/fixedFeederMath";

export type TeachWhich = "first" | "row_end" | "last";

interface Props {
  state: FixedFeederState;
  parts: Part[];
  tapes: Tape[];
  canMove: boolean;
  canActuate: boolean;
  onGoTo: (p: Point) => void;
  onTeach: (which: TeachWhich) => void;
  onPocket: () => void;
  onChanged: (state: FeedersState) => void;
  onRemoved: () => void;
}

/** One strip or tray on the bed: what it holds, where its parts are, and refilling it. */
export function FixedFeederDetail(p: Props) {
  const { state, parts, tapes } = p;
  const [draft, setDraft] = useState<FixedFeeder>(state.feeder);
  const [used, setUsed] = useState<number | null>(0);
  const { run, pending, error } = useAction();
  const f = state.feeder;
  const set = (update: Partial<FixedFeeder>) => setDraft((d) => ({ ...d, ...update }));
  const num = (v: number | null, fallback: number) => (v === null ? fallback : v);
  // The count of parts taken belongs to the core: edits never change it.
  const changed = JSON.stringify({ ...draft, picked: f.picked }) !== JSON.stringify(f);
  const strip = draft.kind === "strip";
  const part = parts.find((x) => x.id === draft.part_id);
  const tape = tapes.find((t) => t.id === part?.tape_id);
  const fmt = (v: number) => v.toFixed(2);

  const refresh = async () => {
    const { data } = await api.GET("/api/v1/feeders");
    if (data) p.onChanged(data);
  };
  const path = { params: { path: { fixed_id: state.id } } };
  const save = async () => {
    const done = await run(() =>
      api.PUT("/api/v1/feeders/fixed/{fixed_id}", {
        ...path,
        body: { ...draft, picked: f.picked },
      }),
    );
    if (done) {
      setDraft(done.feeder);
      await refresh();
    }
  };
  const refill = async () => {
    const done = await run(() =>
      api.POST("/api/v1/feeders/fixed/{fixed_id}/refill", { ...path, body: { picked: used ?? 0 } }),
    );
    if (done) {
      setDraft(done.feeder);
      await refresh();
    }
  };
  const remove = async () => {
    const data = await run(() => api.DELETE("/api/v1/feeders/fixed/{fixed_id}", path));
    if (data) {
      p.onChanged(data);
      p.onRemoved();
    }
  };

  // Teaching, refilling, and picks change the saved feeder: show it (teaching is only
  // offered with no unsaved edits, so none are lost).
  const saved = JSON.stringify(f);
  const shown = useRef(saved);
  useEffect(() => {
    // Keep unsaved edits; otherwise follow the core.
    setDraft((d) => (JSON.stringify(d) === shown.current ? (JSON.parse(saved) as FixedFeeder) : d));
    shown.current = saved;
  }, [saved]);

  return (
    <section className="slot-detail" aria-label={state.id}>
      <header className="slot-detail-head">
        <h2>{state.id}</h2>
        <span className={state.remaining === 0 ? "text-danger" : "text-muted"}>
          {remainingText(state)}
        </span>
      </header>

      <div className="form-grid ff-form">
        <Field label="Name">
          <input
            className="input"
            value={draft.label}
            placeholder="Such as 24 mm holder"
            onChange={(e) => set({ label: e.target.value })}
          />
        </Field>
        <Field label="Kind">
          <select
            className="input"
            value={draft.kind}
            onChange={(e) => set({ kind: e.target.value as FixedFeeder["kind"] })}
          >
            <option value="strip">Tape strip</option>
            <option value="tray">Tray</option>
          </select>
        </Field>
        <Field label="Part">
          <input
            className="input"
            list="fixed-parts"
            value={draft.part_id ?? ""}
            placeholder="Part number from the library"
            onChange={(e) => set({ part_id: e.target.value || null })}
          />
        </Field>
        {strip && (
          <Field label="Takes tape (mm)">
            <WidthSelect value={draft.tape_width} onChange={(v) => set({ tape_width: v })} />
          </Field>
        )}
        <Field label="Pick height">
          <NumberInput value={draft.pick_height} onChange={(v) => set({ pick_height: v })} />
        </Field>
        <Field label={strip ? "Parts in the strip" : "Parts per row"}>
          <NumberInput
            value={draft.columns}
            min={1}
            onChange={(v) => set({ columns: Math.round(num(v, 1)) })}
          />
        </Field>
        <Field label="Rows">
          <NumberInput
            value={draft.rows}
            min={1}
            onChange={(v) => set({ rows: Math.round(num(v, 1)) })}
          />
        </Field>
        <Field label="First part X, Y">
          <span className="input-pair">
            <NumberInput
              value={draft.first.x}
              onChange={(v) => set({ first: { ...draft.first, x: num(v, 0) } })}
            />
            <NumberInput
              value={draft.first.y}
              onChange={(v) => set({ first: { ...draft.first, y: num(v, 0) } })}
            />
          </span>
        </Field>
        <Field label="Next part: step X, Y">
          <span className="input-pair">
            <NumberInput
              value={draft.step.x}
              onChange={(v) => set({ step: { ...draft.step, x: num(v, 0) } })}
            />
            <NumberInput
              value={draft.step.y}
              onChange={(v) => set({ step: { ...draft.step, y: num(v, 0) } })}
            />
          </span>
        </Field>
        {draft.rows > 1 && (
          <Field label="Next row: step X, Y">
            <span className="input-pair">
              <NumberInput
                value={draft.row_step.x}
                onChange={(v) => set({ row_step: { ...draft.row_step, x: num(v, 0) } })}
              />
              <NumberInput
                value={draft.row_step.y}
                onChange={(v) => set({ row_step: { ...draft.row_step, y: num(v, 0) } })}
              />
            </span>
          </Field>
        )}
        {!strip && (
          <Field label="Part angle (°)">
            <NumberInput
              value={draft.part_angle}
              onChange={(v) => set({ part_angle: num(v, 0) })}
            />
          </Field>
        )}
      </div>
      <datalist id="fixed-parts">
        {parts.map((x) => (
          <option key={x.id} value={x.id} />
        ))}
      </datalist>
      {strip && (
        <WidthWarning where="This holder" width={draft.tape_width} part={part} tape={tape} />
      )}
      <label className="check">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        Use it in runs
      </label>
      <p className="text-muted note">
        {strip
          ? `A strip's parts take their angle from the part's tape${tape ? ` (${tape.rotation ?? "not known, taken as 0"}° in ${tape.width ?? "?"} mm tape)` : ", which the library does not give yet"}. Pick from the end where the cover film is peeled back.`
          : "Part angle: the machine angle of the part's 0° direction, the same for every part."}{" "}
        The pick height is where the first pick starts feeling for the part's top, by touch;
        empty, the board height plus the part's height.
      </p>
      <div className="button-row">
        <button
          type="button"
          className="button primary"
          disabled={!changed || pending}
          onClick={() => void save()}
        >
          Save
        </button>
        {changed && (
          <button type="button" className="button" onClick={() => setDraft(f)}>
            Undo changes
          </button>
        )}
      </div>

      <h3 className="detail-heading">Teach with the camera</h3>
      <p className="text-muted note">
        Center the top camera on a part, then say which one it is. Teach the first part
        before the others.
      </p>
      <div className="button-row">
        <button
          type="button"
          className="button"
          disabled={!p.canActuate || changed}
          onClick={() => p.onTeach("first")}
        >
          <Crosshair size={16} aria-hidden />
          First part
        </button>
        <button
          type="button"
          className="button"
          disabled={!p.canActuate || changed || f.columns < 2}
          onClick={() => p.onTeach("row_end")}
        >
          <Crosshair size={16} aria-hidden />
          {f.rows > 1 ? "End of first row" : "Last part"}
        </button>
        {f.rows > 1 && (
          <button
            type="button"
            className="button"
            disabled={!p.canActuate || changed}
            onClick={() => p.onTeach("last")}
          >
            <Crosshair size={16} aria-hidden />
            Last part
          </button>
        )}
      </div>

      <h3 className="detail-heading">Next part</h3>
      {state.next ? (
        <p className="mono">
          number {f.picked + 1} at ({fmt(state.next.x)}, {fmt(state.next.y)})
        </p>
      ) : (
        <p className="text-danger">Empty: refill it.</p>
      )}
      {state.pocket && (
        <p className="text-muted">
          Last pocket check: {fmt(state.pocket.along)} mm along the tape,{" "}
          {fmt(state.pocket.across)} mm across ({state.pocket.holes} sprocket holes).
        </p>
      )}
      <LearnedPicks sourceId={state.id} learned={state.learned} onChanged={p.onChanged} />
      <div className="button-row">
        <button
          type="button"
          className="button"
          disabled={!p.canMove || !state.next}
          onClick={() => state.next && p.onGoTo(state.next)}
        >
          <MoveRight size={16} aria-hidden />
          Go to it
        </button>
        {f.kind === "strip" && (
          <button
            type="button"
            className="button"
            disabled={!p.canMove || !state.next}
            onClick={p.onPocket}
          >
            <ScanSearch size={16} aria-hidden />
            Find pocket
          </button>
        )}
      </div>

      <h3 className="detail-heading">Refill</h3>
      <div className="form-grid">
        <Field label="Parts already gone from it">
          <NumberInput value={used} min={0} onChange={setUsed} />
        </Field>
      </div>
      <div className="button-row">
        <button
          type="button"
          className="button"
          disabled={pending}
          onClick={() => void refill()}
        >
          <RefreshCw size={16} aria-hidden />
          Refilled
        </button>
        <button
          type="button"
          className="button button-warn"
          disabled={pending}
          onClick={() => void remove()}
        >
          <Trash2 size={16} aria-hidden />
          Remove
        </button>
      </div>
      {error && <p className="note text-danger">{error}</p>}
    </section>
  );
}
