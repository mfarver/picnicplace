import {
  Crosshair,
  Eye,
  Focus,
  Lightbulb,
  Map as MapIcon,
  MoveRight,
  Radar,
  Ruler,
  ScanSearch,
  StepBack,
  StepForward,
  Target,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useDescription } from "../api/useDescription";
import { useCameras } from "../api/useCameras";
import { useAction } from "../api/useAction";
import { useMachineControl } from "../api/useMachineControl";
import { WidthSelect, WidthWarning } from "../components/TapeWidth";
import type {
  FeederAssignment,
  FeedersState,
  MachineProfile,
  Part,
  RailState,
  SlotState,
  Tape,
} from "../api/types";
import { CameraView } from "../components/CameraView";
import { pocketMarks, type CameraFootprint } from "../cameraMarks";
import { FixedFeederDetail, type TeachWhich } from "../components/FixedFeederDetail";
import { FixedFeederPanel } from "../components/FixedFeederPanel";
import { Field, NumberInput } from "../components/Form";
import { JogPanel } from "../components/JogPanel";
import { Panel } from "../components/Panel";
import { LearnedPicks } from "../components/PickCorrection";
import { Placeholder } from "../components/Placeholder";
import { MachineView } from "../components/machine/MachineView";
import { useFootprint } from "../useFootprint";

const shortId = (uuid: string) => `${uuid.slice(0, 6)}…${uuid.slice(-6)}`;

/** Feeders: find them on the bus, say which part each holds, and calibrate their slots. */
export function FeedersWorkspace() {
  const description = useDescription();
  const cameras = useCameras();
  const ctl = useMachineControl();
  const { core, machine, canMove, canActuate, run, track, lastOp } = ctl;
  const [state, setState] = useState<FeedersState | null>(null);
  const [parts, setParts] = useState<Part[]>([]);
  const [tapes, setTapes] = useState<Tape[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedFixed, setSelectedFixed] = useState<string | null>(null);
  const [onlyFound, setOnlyFound] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (core.status !== "open") return;
    let cancelled = false;
    void Promise.all([
      api.GET("/api/v1/feeders"),
      api.GET("/api/v1/library/parts"),
      api.GET("/api/v1/library/tapes"),
    ]).then(([feeders, library, tapeList]) => {
      if (cancelled) return;
      if (feeders.data) setState(feeders.data);
      if (library.data) setParts(library.data);
      if (tapeList.data) setTapes(tapeList.data);
    });
    return () => {
      cancelled = true;
    };
  }, [core.status]);

  // A scan, feed, or calibration changes what the core knows: fetch it when one ends.
  const finished = lastOp && lastOp.status !== "running" ? lastOp.id : null;
  useEffect(() => {
    if (!finished) return;
    let cancelled = false;
    void api.GET("/api/v1/feeders").then(({ data }) => {
      if (data && !cancelled) setState(data);
    });
    return () => {
      cancelled = true;
    };
  }, [finished]);

  const slot = state?.slots.find((s) => s.slot.id === selected) ?? null;
  const fixed = state?.fixed.find((f) => f.id === selectedFixed) ?? null;
  const camera = description?.cameras.find((c) => c.mount === "head" && c.looking === "down");
  // Where the top camera is now, to draw what vision found on its picture.
  const cameraAt =
    camera && machine?.position.X !== undefined && machine.position.Y !== undefined
      ? { x: machine.position.X + camera.position.x, y: machine.position.Y + camera.position.y }
      : undefined;

  const cameraTo = (x: number, y: number) => {
    if (!camera) return;
    ctl.moveTo({ X: x - camera.position.x, Y: y - camera.position.y });
  };

  // The part the chosen slot or strip holds, drawn on the camera's picture where the next
  // one is picked, as it should lie in its pocket: pin 1 in red, to compare with the part.
  const shownPart = parts.find(
    (p) => p.id === (slot ? slot.feeder?.assignment?.part_id : fixed?.feeder.part_id),
  );
  const shownFootprint = useFootprint(shownPart?.footprint_id);
  const shownTape = tapes.find((t) => t.id === shownPart?.tape_id);
  const pickFootprint = ((): CameraFootprint | null => {
    if (!shownFootprint || !shownPart) return null;
    const title = `${shownPart.id} as it should lie in its pocket, pin 1 in red`;
    if (slot) {
      const turn = shownTape?.rotation;
      if (turn === null || turn === undefined) return null;
      const angle = slot.slot.rotation + turn;
      return { footprint: shownFootprint, x: slot.pick.x, y: slot.pick.y, angle, title };
    }
    if (fixed && fixed.angle !== null) {
      const at = fixed.next ?? fixed.feeder.first;
      return { footprint: shownFootprint, x: at.x, y: at.y, angle: fixed.angle, title };
    }
    return null;
  })();
  const lookAtSlot = (s: SlotState) => {
    setSelected(s.slot.id);
    setSelectedFixed(null);
    setNotice(null);
    cameraTo(s.pick.x, s.pick.y);
  };
  const lookAtFixed = (id: string) => {
    const f = state?.fixed.find((x) => x.id === id);
    if (!f) return;
    setSelectedFixed(id);
    setSelected(null);
    setNotice(null);
    const at = f.next ?? f.feeder.first;
    cameraTo(at.x, at.y);
  };
  const safeZ = () => {
    if (!description) return;
    const zAxes = new Set(description.nozzles.map((n) => n.z_axis));
    ctl.moveTo(Object.fromEntries([...zAxes].map((axis) => [axis, 0])));
  };

  const park = () => void track(() => api.POST("/api/v1/machine/park"));

  const slotPath = (id: string) => ({ params: { path: { slot_id: id } } });
  const scan = () => void track(() => api.POST("/api/v1/feeders/scan"));
  const feed = (id: string, distance: number | null) =>
    void track(() =>
      api.POST("/api/v1/feeders/slots/{slot_id}/feed", { ...slotPath(id), body: { distance } }),
    );
  const identify = async (id: string) => {
    const result = await run(() =>
      api.POST("/api/v1/feeders/slots/{slot_id}/identify", slotPath(id)),
    );
    if (result) setNotice(result.answered ? "The feeder is flashing its light." : "No answer.");
  };
  const teach = async (id: string) => {
    const data = await run(() =>
      api.PUT("/api/v1/feeders/slots/{slot_id}/pick", { ...slotPath(id), body: { at: null } }),
    );
    if (data) {
      setState(data);
      setNotice("Pick location taught at the camera's position.");
    }
  };
  const forgetPick = async (id: string) => {
    const data = await run(() => api.DELETE("/api/v1/feeders/slots/{slot_id}/pick", slotPath(id)));
    if (data) setState(data);
  };
  const findPocket = (id: string) =>
    void track(() => api.POST("/api/v1/feeders/slots/{slot_id}/pocket", slotPath(id)));
  const measureFiducial = (id: string) =>
    void track(() => api.POST("/api/v1/feeders/slots/{slot_id}/fiducial", slotPath(id)));
  const refresh = async () => {
    const { data } = await api.GET("/api/v1/feeders");
    if (data) setState(data);
  };
  const fixedPath = (id: string) => ({ params: { path: { fixed_id: id } } });
  const addFixed = async () => {
    // Start where the camera is, if the machine knows; the operator teaches the rest.
    const here =
      camera && machine?.homed && machine.position.X !== undefined && machine.position.Y !== undefined
        ? { x: machine.position.X + camera.position.x, y: machine.position.Y + camera.position.y }
        : description
          ? {
              x: (description.bed.x_min + description.bed.x_max) / 2,
              y: (description.bed.y_min + description.bed.y_max) / 2,
            }
          : { x: 0, y: 0 };
    const created = await run(() =>
      api.POST("/api/v1/feeders/fixed", {
        body: {
          label: "",
          kind: "strip",
          part_id: null,
          first: { x: Math.round(here.x * 100) / 100, y: Math.round(here.y * 100) / 100 },
          step: { x: -4, y: 0 },
          columns: 10,
          rows: 1,
          row_step: { x: 0, y: 0 },
          part_angle: 0,
          pick_height: null,
          picked: 0,
          enabled: true,
        },
      }),
    );
    if (!created) return;
    await refresh();
    setSelected(null);
    setSelectedFixed(created.id);
  };
  const teachFixed = async (id: string, which: TeachWhich) => {
    const data = await run(() =>
      api.PUT("/api/v1/feeders/fixed/{fixed_id}/teach", {
        ...fixedPath(id),
        body: { which, at: null },
      }),
    );
    if (data) {
      await refresh();
      setNotice(`${id}: taught at the camera's position.`);
      // A strip's last part says which way its tape feeds: check its sprocket holes are on
      // that side, so one taught the wrong way round is caught now, not in a run.
      if (which === "row_end" && data.feeder.kind === "strip" && data.feeder.rows === 1) {
        void track(() =>
          api.POST("/api/v1/feeders/fixed/{fixed_id}/check-holes", fixedPath(id)),
        );
      }
    }
  };
  const findStripPocket = (id: string) =>
    void track(() => api.POST("/api/v1/feeders/fixed/{fixed_id}/pocket", fixedPath(id)));
  const calibrate = () =>
    void track(() => api.POST("/api/v1/feeders/calibrate", { body: { slot_ids: [] } }));
  const calibrateRails = () =>
    void track(() => api.POST("/api/v1/feeders/rails/calibrate", { body: { bank_ids: [] } }));
  const railFiducialHere = (id: string) =>
    void track(() => api.POST("/api/v1/feeders/slots/{slot_id}/rail-fiducial", slotPath(id)));
  const forgetRail = async (bankId: string) => {
    const data = await run(() =>
      api.DELETE("/api/v1/feeders/rails/{bank_id}", { params: { path: { bank_id: bankId } } }),
    );
    if (data) setState(data);
  };

  const statusLine = ctl.error
    ? { text: ctl.error, danger: true }
    : lastOp && lastOp.status !== "running"
      ? {
          text: lastOp.error ?? lastOp.message ?? `${lastOp.kind}: ${lastOp.status}`,
          danger: lastOp.status === "failed",
        }
      : lastOp
        ? { text: `${lastOp.kind}…`, danger: false }
        : { text: notice ?? "", danger: false };

  const found = state?.slots.filter((s) => s.feeder) ?? [];
  const shown = (state?.slots ?? []).filter((s) => !onlyFound || s.feeder);

  if (!description) {
    return (
      <div className="workspace">
        <div className="canvas">
          <Placeholder
            icon={MapIcon}
            title="Feeders"
            description="Waiting for the machine description from the core."
          />
        </div>
      </div>
    );
  }

  return (
    <div className="workspace">
      <aside className="sidebar">
        {camera && (
          <CameraView
            id={camera.id}
            title={camera.label}
            info={cameras?.find((c) => c.id === camera.id)}
            fieldOfView={camera.field_of_view.x}
            marks={slot ? pocketMarks(slot) : undefined}
            footprints={pickFootprint ? [pickFootprint] : undefined}
            at={cameraAt}
            orientation={description}
          />
        )}

        <JogPanel
          description={description}
          machine={machine}
          canMove={canMove}
          onJog={ctl.jog}
          onSafeZ={safeZ}
          onPark={park}
        />

        <Panel title="Feeder bus">
          {state?.bus ? (
            <>
              <p className="text-muted">
                {state.scanned
                  ? `${found.length} feeder${found.length === 1 ? "" : "s"} found.`
                  : state.restored
                    ? `Not scanned since the core started: showing the ${found.length} the ` +
                      "last scan found. Scan before feeding (a run scans first)."
                    : "Not scanned since the core started."}
              </p>
              <div className="button-row">
                <button
                  type="button"
                  className="button primary"
                  disabled={!canActuate || ctl.pending}
                  onClick={scan}
                >
                  <Radar size={16} aria-hidden />
                  Scan feeders
                </button>
              </div>
            </>
          ) : (
            <p className="text-muted">This machine has no feeder bus.</p>
          )}
          <p className={`note status-line${statusLine.danger ? " text-danger" : " text-muted"}`}>
            {statusLine.text}
          </p>
        </Panel>

        {state && state.rails.length > 0 && (
          <Panel title="Rails" collapseId="feeder-rails">
            <ul className="item-list">
              {state.rails.map((r) => (
                <li key={r.bank} className="run-row">
                  <span className="item-id">{r.label}</span>
                  <span className="text-muted run-where">{railStatus(r)}</span>
                  {r.calibration && (
                    <button
                      type="button"
                      className="button button-sm"
                      disabled={ctl.pending}
                      title="Go back to where the machine profile puts this rail"
                      onClick={() => void forgetRail(r.bank)}
                    >
                      Use profile
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <div className="button-row">
              <button
                type="button"
                className="button"
                disabled={!canMove || !machine?.homed || !state.scanned}
                onClick={calibrateRails}
              >
                <Ruler size={16} aria-hidden />
                Calibrate rails
              </button>
            </div>
            <p className="text-muted note">
              Finds the fiducials of the two feeders farthest apart on each rail and moves its
              slots to where the rail really is, then touches each fiducial with a nozzle for
              the rail's height at that end. Do it once after setting up the machine, then
              <strong> Calibrate all slots</strong>.
            </p>
          </Panel>
        )}

        <Panel title="Slots">
          <label className="check">
            <input
              type="checkbox"
              checked={onlyFound}
              onChange={(e) => setOnlyFound(e.target.checked)}
            />
            Only slots with a feeder
          </label>
          <ul className="item-list slot-list" aria-label="Feeder slots">
            {shown.map((s) => (
              <li key={s.slot.id} className="slot-row">
                <button
                  type="button"
                  className={s.slot.id === selected ? "item active" : "item"}
                  onClick={() => {
                    setSelected(s.slot.id);
                    setSelectedFixed(null);
                    setNotice(null);
                  }}
                >
                  <span className="item-id">
                    <span className={`slot-dot ${slotClass(s)}`} aria-hidden />
                    {s.slot.label}
                  </span>
                  <span className="item-detail">
                    {s.feeder ? (s.feeder.assignment?.part_id ?? "no part assigned") : "empty"}
                    {s.error && <span className="chip chip-warn">error</span>}
                    {!s.error && s.warning && (
                      <span className="chip chip-warn" title={s.warning}>
                        check
                      </span>
                    )}
                  </span>
                </button>
                {s.feeder?.assignment?.part_id && (
                  <button
                    type="button"
                    className="icon-button"
                    disabled={!canMove || !machine?.homed}
                    title={
                      `Look at ${s.slot.label}'s pick with the top camera, ` +
                      "its part's footprint drawn over it"
                    }
                    aria-label={`Look at ${s.slot.label}'s pick`}
                    onClick={() => lookAtSlot(s)}
                  >
                    <Eye size={14} aria-hidden />
                  </button>
                )}
              </li>
            ))}
            {shown.length === 0 && (
              <li className="text-muted item-empty">
                {state?.scanned ? "No feeders found." : "Scan the feeder bus to find feeders."}
              </li>
            )}
          </ul>
        </Panel>

        {state && (
          <FixedFeederPanel
            fixed={state.fixed}
            onChanged={setState}
            selected={selectedFixed}
            onSelect={(id) => {
              setSelectedFixed(id);
              setSelected(null);
              setNotice(null);
            }}
            onAdd={() => void addFixed()}
            onLook={lookAtFixed}
            canLook={canMove && !!machine?.homed}
          />
        )}
      </aside>

      <div className="canvas feeders-canvas">
        <MachineView
          description={description}
          machine={machine}
          canMove={canMove && !!machine?.homed}
          onMoveTo={(p) => cameraTo(p.x, p.y)}
          overlay={state && <SlotOverlay slots={state.slots} selected={selected} />}
          fixedFeeders={state?.fixed}
          selectedFixed={selectedFixed}
        />
        {fixed && (
          <FixedFeederDetail
            tapes={tapes}
            key={fixed.id}
            state={fixed}
            parts={parts}
            canMove={canMove && !!machine?.homed}
            canActuate={canActuate && !ctl.pending}
            onGoTo={(p) => cameraTo(p.x, p.y)}
            onTeach={(which) => void teachFixed(fixed.id, which)}
            onPocket={() => findStripPocket(fixed.id)}
            onChanged={setState}
            onRemoved={() => setSelectedFixed(null)}
          />
        )}
        {slot && (
          <SlotDetail
            tapes={tapes}
            key={slot.slot.id}
            slot={slot}
            state={state!}
            parts={parts}
            description={description}
            canMove={canMove && !!machine?.homed}
            canActuate={canActuate && !ctl.pending}
            onFeed={(d) => feed(slot.slot.id, d)}
            onIdentify={() => void identify(slot.slot.id)}
            onGoTo={() => cameraTo(slot.pick.x, slot.pick.y)}
            onTeach={() => void teach(slot.slot.id)}
            onForget={() => void forgetPick(slot.slot.id)}
            onPocket={() => findPocket(slot.slot.id)}
            onFiducial={() => measureFiducial(slot.slot.id)}
            onCalibrate={calibrate}
            onRailFiducial={() => railFiducialHere(slot.slot.id)}
            onAssigned={(next) => setState(next)}
          />
        )}
      </div>
    </div>
  );
}

/** Where a rail is: the profile's place, or how far it was found from it. */
function railStatus(r: RailState): string {
  const c = r.calibration;
  if (!c) return "where the profile puts it";
  const found = Object.keys(c.points).length;
  if (!c.fitted) return `${found} fiducial${found === 1 ? "" : "s"} found; needs two far apart`;
  const moved = Math.hypot(c.shift.x, c.shift.y).toFixed(2);
  return `placed: ${moved} mm off the profile, turned ${c.angle.toFixed(2)}°${railLevel(r)}`;
}

/** The rail's height where its feeders' noses were touched, end to end. */
function railLevel(r: RailState): string {
  const touched = Object.entries(r.calibration?.points ?? {})
    .filter(([id, p]) => /^\d+$/.test(id) && p.z != null)
    .sort(([a], [b]) => Number(a) - Number(b));
  const head = touched.at(0);
  const tail = touched.at(-1);
  if (!head || !tail) return "";
  const [firstId, first] = head;
  const [lastId, last] = tail;
  const z1 = first.z ?? 0;
  const z2 = last.z ?? 0;
  if (firstId === lastId) return `; nose at Z ${z1.toFixed(2)} (slot ${firstId})`;
  const run = Math.hypot(last.found.x - first.found.x, last.found.y - first.found.y);
  const per100 = run > 1 ? ((z2 - z1) / run) * 100 : 0;
  return (
    `; noses at Z ${z1.toFixed(2)} (slot ${firstId}) and ${z2.toFixed(2)} ` +
    `(slot ${lastId}), ${Math.abs(per100).toFixed(2)} mm per 100 mm`
  );
}

function slotClass(s: SlotState): string {
  if (s.error) return "slot-error";
  if (!s.feeder) return "slot-empty";
  return s.feeder.assignment?.part_id ? "slot-ready" : "slot-unassigned";
}

/** Pick locations on the map: colored by state, the selected one ringed. */
function SlotOverlay({ slots, selected }: { slots: SlotState[]; selected: string | null }) {
  return (
    <g className="fd-overlay">
      {slots.map((s) => (
        <g key={s.slot.id} transform={`translate(${s.pick.x} ${-s.pick.y})`}>
          <circle className={`fd-pick ${slotClass(s)}`} r={s.feeder ? 2.4 : 1.4}>
            <title>{s.slot.label}</title>
          </circle>
          {s.slot.id === selected && <circle className="fd-selected" r={5} />}
          {s.pocket && (
            <circle
              className="fd-pocket"
              cx={s.pocket.at.x - s.pick.x}
              cy={-(s.pocket.at.y - s.pick.y)}
              r={1}
            />
          )}
        </g>
      ))}
    </g>
  );
}

/**
 * How many parts are on a feeder's reel, counted down by each feed, and the level at which
 * a popup warns that it is running low. Set from the reel's label when it is loaded.
 */
function ReelCount({
  uuid,
  assignment,
  onSaved,
}: {
  uuid: string;
  assignment: FeederAssignment | null;
  onSaved: (state: FeedersState) => void;
}) {
  const [left, setLeft] = useState<number | null>(assignment?.reel_left ?? null);
  const [low, setLow] = useState<number | null>(assignment?.reel_low ?? null);
  const { run, pending: saving, error } = useAction();
  // The count shown below is the live one; these are what the operator types.
  const counted = assignment?.reel_left;
  const held = !!assignment?.part_id;

  const save = async () => {
    const whole = (v: number | null) => (v == null ? null : Math.round(v));
    const data = await run(() =>
      api.PUT("/api/v1/feeders/assignments/{feeder_id}/reel", {
        params: { path: { feeder_id: uuid } },
        body: { left: whole(left), low: whole(low) },
      }),
    );
    if (data) onSaved(data);
  };

  return (
    <>
      <h3 className="detail-heading">Reel</h3>
      <div className="form-grid">
        <Field label="Parts on reel">
          <NumberInput value={left} min={0} onChange={setLeft} />
        </Field>
        <Field label="Warn at">
          <NumberInput value={low} min={0} onChange={setLow} />
        </Field>
      </div>
      <p className="text-muted note">
        {counted == null
          ? "Not counted. Enter the count from the reel's label: each feed takes one off."
          : `${counted} left by its count${
              assignment?.reel_low != null && counted <= assignment.reel_low
                ? ", at or under its warning level"
                : ""
            }. Each feed takes one off; a popup warns at the level set.`}
      </p>
      {error && <p className="note note-danger">{error}</p>}
      <div className="button-row">
        <button
          type="button"
          className="button"
          disabled={saving || !held}
          title={held ? undefined : "Say which part the feeder holds first"}
          onClick={() => void save()}
        >
          Save reel
        </button>
      </div>
    </>
  );
}

interface DetailProps {
  slot: SlotState;
  state: FeedersState;
  parts: Part[];
  tapes: Tape[];
  description: MachineProfile;
  canMove: boolean;
  canActuate: boolean;
  onFeed: (distance: number | null) => void;
  onIdentify: () => void;
  onGoTo: () => void;
  onTeach: () => void;
  onForget: () => void;
  onPocket: () => void;
  onFiducial: () => void;
  onCalibrate: () => void;
  onRailFiducial: () => void;
  onAssigned: (state: FeedersState) => void;
}

function SlotDetail(p: DetailProps) {
  const { slot, state, parts, tapes } = p;
  const feeder = slot.feeder;
  const [partId, setPartId] = useState(feeder?.assignment?.part_id ?? "");
  const [pitch, setPitch] = useState<number | null>(feeder?.assignment?.part_pitch ?? null);
  const [width, setWidth] = useState<number | null>(feeder?.assignment?.tape_width ?? null);
  const [saving, setSaving] = useState(false);
  const part = useMemo(() => parts.find((x) => x.id === partId), [parts, partId]);
  const tape = tapes.find((t) => t.id === part?.tape_id);
  const step = pitch ?? tape?.pitch ?? 4;
  const cal = slot.calibration;
  const offset = state.fiducial_offset;
  const rail = state.rails.find((r) => r.bank === slot.slot.bank);
  const fmt = (v: number) => v.toFixed(2);

  const save = async () => {
    if (!feeder) return;
    setSaving(true);
    const { data } = await api.PUT("/api/v1/feeders/assignments/{feeder_id}", {
      params: { path: { feeder_id: feeder.uuid } },
      body: {
        part_id: partId || null,
        part_pitch: pitch,
        tape_width: width,
        enabled: feeder.assignment?.enabled ?? true,
        note: feeder.assignment?.note ?? "",
      },
    });
    setSaving(false);
    if (data) p.onAssigned(data);
  };

  return (
    <section className="slot-detail" aria-label={slot.slot.label}>
      <header className="slot-detail-head">
        <h2>{slot.slot.label}</h2>
        <span className="text-muted">
          {p.description.feeder_banks.find((b) => b.id === slot.slot.bank)?.label}
          {slot.slot.address !== null && slot.slot.address !== slot.slot.number
            ? `, address ${slot.slot.address}`
            : ""}
        </span>
      </header>

      {slot.error && <p className="note note-danger">{slot.error}</p>}

      <h3 className="detail-heading">Feeder</h3>
      {feeder ? (
        <>
          <p className="mono text-muted" title={feeder.uuid}>
            {shortId(feeder.uuid)} {feeder.initialized ? "" : "(not initialized)"}
          </p>
          <div className="form-grid">
            <Field label="Part">
              <input
                className="input"
                list="feeder-parts"
                value={partId}
                placeholder="Part number from the library"
                onChange={(e) => setPartId(e.target.value)}
              />
            </Field>
            <Field label="Part pitch (mm)">
              <NumberInput value={pitch} min={0.1} onChange={setPitch} />
            </Field>
            <Field label="Takes tape (mm)">
              <WidthSelect value={width} onChange={setWidth} />
            </Field>
          </div>
          <datalist id="feeder-parts">
            {parts.map((x) => (
              <option key={x.id} value={x.id} />
            ))}
          </datalist>
          <TapeInUse part={part} tape={tape} />
          <WidthWarning where="This feeder" width={width} part={part} tape={tape} />
          <p className="text-muted note">
            {pitch !== null
              ? `Feeds ${pitch} mm: this feeder's own setting, over the tape's.`
              : tape?.pitch
                ? `Feeds ${tape.pitch} mm: from the tape. Set a pitch here only to override it.`
                : "Feeds 4 mm: neither this feeder nor the part's tape gives a pitch."}
          </p>
          <div className="button-row">
            <button type="button" className="button" disabled={saving} onClick={() => void save()}>
              Save
            </button>
            <button
              type="button"
              className="button"
              disabled={!p.canActuate}
              onClick={() => p.onFeed(null)}
            >
              <StepForward size={16} aria-hidden />
              Feed {step} mm
            </button>
            <button
              type="button"
              className="button"
              disabled={!p.canActuate}
              onClick={() => p.onFeed(-step)}
            >
              <StepBack size={16} aria-hidden />
              Back
            </button>
            <button
              type="button"
              className="button"
              disabled={!p.canActuate}
              onClick={p.onIdentify}
            >
              <Lightbulb size={16} aria-hidden />
              Identify
            </button>
          </div>
          <ReelCount
            key={`${feeder.uuid}:${feeder.assignment?.part_id ?? ""}`}
            uuid={feeder.uuid}
            assignment={feeder.assignment ?? null}
            onSaved={p.onAssigned}
          />
        </>
      ) : (
        <p className="text-muted">No feeder found here. Scan the feeder bus after adding one.</p>
      )}

      <h3 className="detail-heading">Pick location</h3>
      <p className="mono">
        ({fmt(slot.pick.x)}, {fmt(slot.pick.y)}){" "}
        <span className="text-muted">
          {cal
            ? `${cal.basis === "taught" ? "taught" : "from its feeder's fiducial"}, ${fmt(cal.correction.x)}, ${fmt(cal.correction.y)} from nominal`
            : "nominal, from the machine profile"}
        </span>
      </p>
      {slot.pocket && (
        <p className="text-muted">
          Last pocket check: {fmt(slot.pocket.along)} mm along the feed,{" "}
          {fmt(slot.pocket.across)} mm across ({slot.pocket.holes} sprocket holes).
        </p>
      )}
      <LearnedPicks sourceId={slot.slot.id} learned={slot.learned} onChanged={p.onAssigned} />
      <div className="button-row">
        <button type="button" className="button" disabled={!p.canMove} onClick={p.onGoTo}>
          <MoveRight size={16} aria-hidden />
          Go to pick
        </button>
        <button type="button" className="button" disabled={!p.canMove} onClick={p.onPocket}>
          <ScanSearch size={16} aria-hidden />
          Find pocket
        </button>
        <button type="button" className="button" disabled={!p.canActuate} onClick={p.onTeach}>
          <Crosshair size={16} aria-hidden />
          Pick is here
        </button>
        {cal && (
          <button type="button" className="button" onClick={p.onForget}>
            Forget
          </button>
        )}
      </div>
      <p className="text-muted note">
        <strong>Find pocket</strong> looks for the tape's sprocket holes and centers the camera on
        the pocket they put the next part in. <strong>Pick is here</strong> teaches the pick
        location at the camera's center: jog it over the pocket first.
      </p>

      <h3 className="detail-heading">Feeder fiducial</h3>
      <p className="text-muted">
        {offset
          ? `The fiducial is ${fmt(offset.x)} mm along the feed from the pick location and ${fmt(offset.y)} mm across from the sprocket holes (${state.fiducial_offset_source === "measured" ? "measured" : "from the profile"}). Measure it on one feeder to replace the design value.`
          : "Not measured yet. Teach this slot's pick location, center the camera on the feeder's fiducial, then measure it here."}
      </p>
      <div className="button-row">
        <button type="button" className="button" disabled={!p.canMove} onClick={p.onFiducial}>
          <Focus size={16} aria-hidden />
          Measure fiducial here
        </button>
        <button
          type="button"
          className="button"
          disabled={!p.canMove || !offset}
          onClick={p.onCalibrate}
        >
          <Target size={16} aria-hidden />
          Calibrate all slots
        </button>
      </div>
      {rail && !rail.calibration?.fitted && (
        <>
          <p className="text-muted note">
            If <strong>Calibrate rails</strong> could not find this feeder's fiducial, jog the
            camera onto it and click <strong>Rail fiducial is here</strong>.
          </p>
          <div className="button-row">
            <button
              type="button"
              className="button"
              disabled={!p.canMove}
              onClick={p.onRailFiducial}
            >
              <Ruler size={16} aria-hidden />
              Rail fiducial is here
            </button>
          </div>
        </>
      )}
    </section>
  );
}

/** The tape a feeder's part comes in, and the values a pick takes from it. */
export function TapeInUse({ part, tape }: { part?: Part; tape?: Tape }) {
  if (!part) return null;
  if (!tape) {
    return (
      <p className="note text-warn">
        {part.id} has no tape in the library: picks take its rotation in tape as 0°. Give it one
        in the Parts workspace.
      </p>
    );
  }
  const rotation = tape.rotation === null ? "not known (taken as 0°)" : `${tape.rotation}°`;
  return (
    <p className="text-muted note">
      Tape (from {part.id}): {tape.name || tape.id}. Rotation in tape {rotation}, width{" "}
      {tape.width ?? "?"} mm, pitch {tape.pitch ?? "?"} mm. A pick turns the part by the slot's
      orientation plus the rotation in tape.
    </p>
  );
}
