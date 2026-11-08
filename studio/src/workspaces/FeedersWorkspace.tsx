import {
  Crosshair,
  Focus,
  Lightbulb,
  Map as MapIcon,
  MoveRight,
  Radar,
  ScanSearch,
  StepBack,
  StepForward,
  Target,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useDescription } from "../api/useDescription";
import { useCameras } from "../api/useCameras";
import { useMachineControl } from "../api/useMachineControl";
import type { FeedersState, MachineProfile, Part, SlotState } from "../api/types";
import { CameraView } from "../components/CameraView";
import { Field, NumberInput } from "../components/Form";
import { JogPanel } from "../components/JogPanel";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";
import { MachineView } from "../components/machine/MachineView";

const shortId = (uuid: string) => `${uuid.slice(0, 6)}…${uuid.slice(-6)}`;

/** Feeders: find them on the bus, say which part each holds, and calibrate their slots. */
export function FeedersWorkspace() {
  const description = useDescription();
  const cameras = useCameras();
  const ctl = useMachineControl();
  const { core, machine, canMove, canActuate, run, track, lastOp } = ctl;
  const [state, setState] = useState<FeedersState | null>(null);
  const [parts, setParts] = useState<Part[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [onlyFound, setOnlyFound] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (core.status !== "open") return;
    let cancelled = false;
    void Promise.all([api.GET("/api/v1/feeders"), api.GET("/api/v1/library/parts")]).then(
      ([feeders, library]) => {
        if (cancelled) return;
        if (feeders.data) setState(feeders.data);
        if (library.data) setParts(library.data);
      },
    );
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
  const camera = description?.cameras.find((c) => c.mount === "head" && c.looking === "down");

  const cameraTo = (x: number, y: number) => {
    if (!camera) return;
    ctl.moveTo({ X: x - camera.position.x, Y: y - camera.position.y });
  };
  const safeZ = () => {
    if (!description) return;
    const zAxes = new Set(description.nozzles.map((n) => n.z_axis));
    ctl.moveTo(Object.fromEntries([...zAxes].map((axis) => [axis, 0])));
  };

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
  const calibrate = () =>
    void track(() => api.POST("/api/v1/feeders/calibrate", { body: { slot_ids: [] } }));

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
          />
        )}

        <Panel title="Feeder bus">
          {state?.bus ? (
            <>
              <p className="text-muted">
                {state.scanned
                  ? `${found.length} feeder${found.length === 1 ? "" : "s"} found.`
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
              <li key={s.slot.id}>
                <button
                  type="button"
                  className={s.slot.id === selected ? "item active" : "item"}
                  onClick={() => {
                    setSelected(s.slot.id);
                    setNotice(null);
                  }}
                >
                  <span className="item-id">
                    <span className={`slot-dot ${slotClass(s)}`} aria-hidden />
                    {s.slot.label}
                  </span>
                  <span className="item-detail">
                    {s.feeder
                      ? (s.feeder.assignment?.part_id ?? "no part assigned")
                      : s.slot.address !== null
                        ? `address ${s.slot.address}`
                        : ""}
                    {s.error && <span className="chip chip-warn">error</span>}
                  </span>
                </button>
              </li>
            ))}
            {shown.length === 0 && (
              <li className="text-muted item-empty">
                {state?.scanned ? "No feeders found." : "Scan the feeder bus to find feeders."}
              </li>
            )}
          </ul>
        </Panel>

        <JogPanel
          description={description}
          machine={machine}
          canMove={canMove}
          onJog={ctl.jog}
          onSafeZ={safeZ}
        />
      </aside>

      <div className="canvas feeders-canvas">
        <MachineView
          description={description}
          machine={machine}
          canMove={canMove && !!machine?.homed}
          onMoveTo={(p) => cameraTo(p.x, p.y)}
          overlay={state && <SlotOverlay slots={state.slots} selected={selected} />}
        />
        {slot && (
          <SlotDetail
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
            onAssigned={(next) => setState(next)}
          />
        )}
      </div>
    </div>
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

interface DetailProps {
  slot: SlotState;
  state: FeedersState;
  parts: Part[];
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
  onAssigned: (state: FeedersState) => void;
}

function SlotDetail(p: DetailProps) {
  const { slot, state, parts } = p;
  const feeder = slot.feeder;
  const [partId, setPartId] = useState(feeder?.assignment?.part_id ?? "");
  const [pitch, setPitch] = useState<number | null>(feeder?.assignment?.part_pitch ?? null);
  const [saving, setSaving] = useState(false);
  const part = useMemo(() => parts.find((x) => x.id === partId), [parts, partId]);
  const step = pitch ?? part?.tape?.pitch ?? 4;
  const cal = slot.calibration;
  const offset = state.fiducial_offset;
  const fmt = (v: number) => v.toFixed(2);

  const save = async () => {
    if (!feeder) return;
    setSaving(true);
    const { data } = await api.PUT("/api/v1/feeders/assignments/{feeder_id}", {
      params: { path: { feeder_id: feeder.uuid } },
      body: {
        part_id: partId || null,
        part_pitch: pitch,
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
        <span className="text-muted mono">
          {slot.slot.address !== null ? `address ${slot.slot.address}` : ""}
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
          </div>
          <datalist id="feeder-parts">
            {parts.map((x) => (
              <option key={x.id} value={x.id} />
            ))}
          </datalist>
          <p className="text-muted note">
            {pitch === null && part?.tape?.pitch
              ? `Empty pitch: the part's tape pitch, ${part.tape.pitch} mm.`
              : pitch === null
                ? "Empty pitch: 4 mm, unless the part's tape says otherwise."
                : ""}
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
        </>
      ) : (
        <p className="text-muted">No feeder found here. Scan the feeder bus after adding one.</p>
      )}

      <h3 className="detail-heading">Pick location</h3>
      <p className="mono">
        ({fmt(slot.pick.x)}, {fmt(slot.pick.y)}){" "}
        <span className="text-muted">
          {cal
            ? `${cal.basis === "taught" ? "taught" : "from the fiducial"}, ${fmt(cal.correction.x)}, ${fmt(cal.correction.y)} from nominal`
            : "nominal, from the machine profile"}
        </span>
      </p>
      {slot.pocket && (
        <p className="text-muted">
          Last pocket check: {fmt(slot.pocket.along)} mm along the feed,{" "}
          {fmt(slot.pocket.across)} mm across ({slot.pocket.holes} sprocket holes).
        </p>
      )}
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
          ? `The fiducial is ${fmt(offset.x)} mm along the feed and ${fmt(offset.y)} mm across it from the pick location (${state.fiducial_offset_source === "measured" ? "measured" : "from the profile"}).`
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
    </section>
  );
}
