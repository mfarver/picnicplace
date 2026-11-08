import { ArrowDownToLine, Crosshair, Hand, ScanEye, Trash2, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import type {
  FeedersState,
  MachineProfile,
  MachineState,
  NozzleLoad,
  Operation,
  PlacementState,
} from "../api/types";
import { Field, NumberInput } from "./Form";
import { Panel } from "./Panel";

const KINDS = new Set(["pick", "align", "place", "pick_and_place", "discard"]);

function describe(load: NozzleLoad): string {
  const what = load.part_id ?? "a part";
  const a = load.alignment;
  const aligned = a
    ? `aligned: ${Math.hypot(a.dx, a.dy).toFixed(2)} mm off center`
    : "not aligned yet";
  return `${what} from slot ${load.slot_id}, ${aligned}`;
}

/**
 * One part at a time, one step at a time: pick from a feeder slot, find it on the
 * bottom camera, and place it at a typed-in location (or where the camera is).
 */
export function PlacementPanel({
  description,
  machine,
  canMove,
}: {
  description: MachineProfile;
  machine?: MachineState;
  canMove: boolean;
}) {
  const { status, operations } = useCore();
  const { run, pending, error } = useAction();
  const [lastOpId, setLastOpId] = useState<string | null>(null);
  const [feeders, setFeeders] = useState<FeedersState | null>(null);
  const [placement, setPlacement] = useState<PlacementState | null>(null);
  const [nozzle, setNozzle] = useState(description.nozzles[0]?.id ?? "");
  const [slot, setSlot] = useState("");
  const [feed, setFeed] = useState(true);
  const [findPocket, setFindPocket] = useState(true);
  const [x, setX] = useState<number | null>(null);
  const [y, setY] = useState<number | null>(null);
  const [rotation, setRotation] = useState<number | null>(0);

  const lastOp = lastOpId ? operations[lastOpId] : undefined;
  // Any finished operation may have changed the feeders or the nozzles (a scan started
  // elsewhere, say): fetch again whenever the latest finish time changes.
  const finished = Object.values(operations).reduce(
    (latest, o) => (o.finished_at && o.finished_at > latest ? o.finished_at : latest),
    "",
  );

  // Fetch feeders and nozzle loads when connected, and after operations finish.
  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void Promise.all([api.GET("/api/v1/feeders"), api.GET("/api/v1/placement")]).then(
      ([f, p]) => {
        if (cancelled) return;
        if (f.data) setFeeders(f.data);
        if (p.data) setPlacement(p.data);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [status, finished]);

  const slots = (feeders?.slots ?? []).filter((s) => s.feeder);
  const chosenSlot = slot || slots[0]?.slot.id || "";
  const camera = description.cameras.find((c) => c.mount === "head" && c.looking === "down");
  const here = () => {
    if (!machine || !camera) return;
    setX(Number(((machine.position.X ?? 0) + camera.position.x).toFixed(3)));
    setY(Number(((machine.position.Y ?? 0) + camera.position.y).toFixed(3)));
  };

  const start = async (call: () => Promise<{ data?: Operation; error?: unknown }>) => {
    const op = await run(call);
    if (op) setLastOpId(op.id);
  };
  const busy = pending || lastOp?.status === "running";
  const ready = canMove && !busy;
  const target = x !== null && y !== null;
  const loads = placement?.loads ?? [];
  const load = loads.find((l) => l.nozzle === nozzle);

  const pick = { slot_id: chosenSlot, nozzle_id: nozzle, feed, find_pocket: findPocket };
  const place = { nozzle_id: nozzle, x: x ?? 0, y: y ?? 0, rotation: rotation ?? 0 };

  const result =
    error ??
    (lastOp && KINDS.has(lastOp.kind)
      ? lastOp.status === "running"
        ? `${lastOp.kind.replaceAll("_", " ")}…`
        : (lastOp.error ?? lastOp.message ?? lastOp.status)
      : null);

  return (
    <Panel title="Single placement" collapseId="placement">
      <div className="form-grid placement-grid">
        <Field label="Nozzle">
          <select className="input" value={nozzle} onChange={(e) => setNozzle(e.target.value)}>
            {description.nozzles.map((n) => (
              <option key={n.id} value={n.id}>
                {n.id} ({n.label.toLowerCase()})
              </option>
            ))}
          </select>
        </Field>
        <Field label="Feeder">
          <select
            className="input"
            value={chosenSlot}
            onChange={(e) => setSlot(e.target.value)}
            disabled={slots.length === 0}
          >
            {slots.length === 0 && <option value="">Scan the feeders first</option>}
            {slots.map((s) => (
              <option key={s.slot.id} value={s.slot.id}>
                {s.slot.label}: {s.feeder?.assignment?.part_id ?? "no part"}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="bringup-row">
        <label className="check">
          <input type="checkbox" checked={feed} onChange={(e) => setFeed(e.target.checked)} />
          Feed first
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={findPocket}
            onChange={(e) => setFindPocket(e.target.checked)}
          />
          Find the pocket
        </label>
      </div>
      <div className="placement-target">
        <Field label="X">
          <NumberInput value={x} onChange={setX} />
        </Field>
        <Field label="Y">
          <NumberInput value={y} onChange={setY} />
        </Field>
        <Field label="Angle">
          <NumberInput value={rotation} onChange={setRotation} />
        </Field>
        <button
          type="button"
          className="button button-icon"
          title="Use the top camera's position"
          aria-label="Use the top camera's position"
          disabled={!machine?.homed}
          onClick={here}
        >
          <Crosshair size={16} aria-hidden />
        </button>
      </div>
      <div className="button-row">
        <button
          type="button"
          className="button"
          disabled={!ready || !chosenSlot || !!load}
          onClick={() => void start(() => api.POST("/api/v1/placement/pick", { body: pick }))}
        >
          <Hand size={16} aria-hidden />
          Pick
        </button>
        <button
          type="button"
          className="button"
          disabled={!ready || !load}
          onClick={() =>
            void start(() =>
              api.POST("/api/v1/placement/align", { body: { nozzle_id: nozzle } }),
            )
          }
        >
          <ScanEye size={16} aria-hidden />
          Align
        </button>
        <button
          type="button"
          className="button"
          disabled={!ready || !load?.alignment || !target}
          onClick={() => void start(() => api.POST("/api/v1/placement/place", { body: place }))}
        >
          <ArrowDownToLine size={16} aria-hidden />
          Place
        </button>
        <button
          type="button"
          className="button primary"
          disabled={!ready || !chosenSlot || !!load || !target}
          onClick={() =>
            void start(() =>
              api.POST("/api/v1/placement/pick-and-place", { body: { ...pick, ...place } }),
            )
          }
        >
          <Zap size={16} aria-hidden />
          All three
        </button>
        {load && (
          <button
            type="button"
            className="button"
            disabled={!ready}
            onClick={() =>
              void start(() =>
                api.POST("/api/v1/placement/discard", { body: { nozzle_id: nozzle } }),
              )
            }
          >
            <Trash2 size={16} aria-hidden />
            Discard
          </button>
        )}
      </div>
      {loads.map((l) => (
        <p key={l.nozzle} className="note text-muted">
          <strong>{l.nozzle}</strong> holds {describe(l)}.
        </p>
      ))}
      {result && (
        <p
          className={`note${error || lastOp?.status === "failed" ? " text-danger" : " text-muted"}`}
        >
          {result}
        </p>
      )}
    </Panel>
  );
}
