import { Check, ChevronDown, ChevronRight, CircleAlert, Copy, MapPin, X } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import type { BringUpRecord, MachineProfile, MachineState, StepRecord } from "../api/types";
import { Panel } from "./Panel";

type Status = StepRecord["status"];
type Values = StepRecord["values"];

interface Actions {
  canMove: boolean;
  onHome: () => void;
  onJog: (axis: string, distance: number) => void;
  onVisualHome: () => void;
  onCameraScale: () => void;
  onScanFeeders: () => void;
}

interface StepContext extends Actions {
  description: MachineProfile;
  machine?: MachineState;
  values: Values;
  setValue: (key: string, value: string | number | boolean) => void;
}

interface Step {
  id: string;
  title: string;
  body: (c: StepContext) => ReactNode;
  /** A check the Studio can make by itself, shown beside the step. */
  check?: (c: StepContext) => boolean | null;
}

/** Two or three buttons that record one answer. */
function Choice({
  c,
  name,
  options,
}: {
  c: StepContext;
  name: string;
  options: [string, string][];
}) {
  return (
    <div className="segmented">
      {options.map(([value, label]) => (
        <button
          key={value}
          type="button"
          className={c.values[name] === value ? "segment active" : "segment"}
          onClick={() => c.setValue(name, value)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

const STEPS: Step[] = [
  {
    id: "probe",
    title: "Probe the firmware",
    body: () => (
      <p>
        Before starting the core on the machine, run{" "}
        <code>uv run picnicplace-probe --serial-port /dev/ttyACM0 --with-pumps</code> in{" "}
        <code>core</code>. It never moves the machine. Keep its transcript.
      </p>
    ),
  },
  {
    id: "slow",
    title: "Slow speed and a hand on the E-stop",
    body: (c) => (
      <p>
        Set <code>speed_scale = 0.25</code> under <code>[machine]</code> while checking the
        machine. Now: {Math.round((c.machine?.speed_scale ?? 1) * 100)}%. Keep a hand on the
        physical E-stop throughout.
      </p>
    ),
    check: (c) => (c.machine ? c.machine.speed_scale <= 0.25 : null),
  },
  {
    id: "home",
    title: "Home",
    body: (c) => (
      <>
        <p>
          Watch every axis reach its switch. After homing, Z should read 0 (both nozzles up).
          {c.machine?.homed && ` Z reads ${c.machine.position.Z?.toFixed(3) ?? "?"}.`}
        </p>
        <button type="button" className="button" disabled={!c.canMove} onClick={c.onHome}>
          Home
        </button>
      </>
    ),
    check: (c) => (c.machine?.homed ? Math.abs(c.machine.position.Z ?? 1) < 0.01 : null),
  },
  {
    id: "jog",
    title: "Which way the axes point",
    body: (c) => (
      <>
        <p>Jog 10 mm and say which way the head moved, as seen from the front of the machine.</p>
        <div className="bringup-row">
          <button
            type="button"
            className="button"
            disabled={!c.canMove || !c.machine?.homed}
            onClick={() => c.onJog("X", 10)}
          >
            +X 10 mm
          </button>
          <Choice
            c={c}
            name="x_points"
            options={[
              ["left", "Left"],
              ["right", "Right"],
            ]}
          />
        </div>
        <div className="bringup-row">
          <button
            type="button"
            className="button"
            disabled={!c.canMove || !c.machine?.homed}
            onClick={() => c.onJog("Y", 10)}
          >
            +Y 10 mm
          </button>
          <Choice
            c={c}
            name="y_points"
            options={[
              ["front", "Front"],
              ["back", "Back"],
            ]}
          />
        </div>
      </>
    ),
    check: (c) =>
      c.values.x_points && c.values.y_points
        ? c.values.x_points === c.description.x_points &&
          c.values.y_points === c.description.y_points
        : null,
  },
  {
    id: "nozzles",
    title: "Which nozzle is which",
    body: (c) => {
      const n1 = c.description.nozzles[0];
      return (
        <>
          <p>
            Jog Z down 1 mm (−Z) and back up. Which nozzle went down? The profile says that is{" "}
            {n1 ? `${n1.id}, the ${n1.label.toLowerCase()}` : "the first nozzle"}.
          </p>
          <div className="bringup-row">
            <button
              type="button"
              className="button"
              disabled={!c.canMove || !c.machine?.homed}
              onClick={() => c.onJog("Z", -1)}
            >
              Z −1 mm
            </button>
            <button
              type="button"
              className="button"
              disabled={!c.canMove || !c.machine?.homed}
              onClick={() => c.onJog("Z", 1)}
            >
              Z +1 mm
            </button>
            <Choice
              c={c}
              name="down_nozzle"
              options={[
                ["left", "Left"],
                ["right", "Right"],
              ]}
            />
          </div>
        </>
      );
    },
    check: (c) => {
      const label = c.description.nozzles[0]?.label.toLowerCase() ?? "";
      return c.values.down_nozzle ? label.startsWith(String(c.values.down_nozzle)) : null;
    },
  },
  {
    id: "outputs",
    title: "Lights, pumps, and valves",
    body: () => (
      <p>
        Turn each light, pump, and valve on and off from the head inset on the map. Each pump
        with its valve open should pull the nozzle's vacuum reading well away from rest.
      </p>
    ),
  },
  {
    id: "stop",
    title: "STOP during a move",
    body: () => (
      <p>
        Click far across the map to start a slow move, then press STOP. Note whether it stops at
        once, and what it took to recover (Reset, the board's reset button, or a power cycle).
      </p>
    ),
  },
  {
    id: "camera",
    title: "Top camera orientation",
    body: (c) => (
      <p>
        Jog +X: the picture should slide {c.description.x_points === "right" ? "left" : "right"}.
        Jog +Y: it should slide {c.description.y_points === "back" ? "down" : "up"}. If not, set{" "}
        <code>flip_x</code>, <code>flip_y</code>, or <code>rotation</code> under{" "}
        <code>[cameras.top]</code>. The bottom camera looks up, so it shows the nozzle mirrored:
        with a nozzle over it, jog +X and the nozzle should move{" "}
        {c.description.x_points === "right" ? "left" : "right"} in its picture, and +Y{" "}
        {c.description.y_points === "back" ? "up" : "down"}; if not, set the flips under{" "}
        <code>[cameras.bottom]</code>. Save a few frames with the Capture panel.
      </p>
    ),
  },
  {
    id: "visual_home",
    title: "Visual homing and camera scale",
    body: (c) => (
      <>
        <p>
          Visual homing should find the datum fiducial and correct well under a millimeter. Camera
          scale should come out close to the profile's field of view.
        </p>
        <div className="button-row">
          <button
            type="button"
            className="button"
            disabled={!c.canMove || !c.machine?.homed}
            onClick={c.onVisualHome}
          >
            Visual home
          </button>
          <button
            type="button"
            className="button"
            disabled={!c.canMove || !c.machine?.homed}
            onClick={c.onCameraScale}
          >
            Camera scale
          </button>
        </div>
      </>
    ),
  },
  {
    id: "feeders",
    title: "Feeders",
    body: (c) => (
      <>
        <p>
          Scan the feeder bus. Then, in the Feeders workspace: feed one feeder, center the camera
          over its pocket and teach the pick location, center it on the feeder's fiducial and
          measure it, and calibrate all slots from their fiducials. Finally try Find pocket on a
          few slots.
        </p>
        <button
          type="button"
          className="button"
          disabled={!c.canMove}
          onClick={c.onScanFeeders}
        >
          Scan feeders
        </button>
      </>
    ),
  },
  {
    id: "first_part",
    title: "One part, one step at a time",
    body: () => (
      <p>
        Put a scrap board on the plate and give a feeder its part (with its height). In the
        Single placement panel, Pick, then Align, then Place on the board, checking each step.
        Capture the bottom camera while a part is held, so alignment can be tuned on real images.
      </p>
    ),
  },
  {
    id: "keep_outs",
    title: "Measure the keep-outs and plates",
    body: () => (
      <p>
        Center the camera on the corners of the datum board, the camera opening, the aiming
        fiducial's holder, and the rear staging plate, and record each point below. Points whose
        labels start with the same word (such as "datum front left" and "datum back right")
        become one rectangle in the suggested changes.
      </p>
    ),
  },
  {
    id: "full_speed",
    title: "Full speed",
    body: () => (
      <p>
        When everything above checks out, remove <code>speed_scale</code> from the config (or set
        it to 1.0) and restart the core.
      </p>
    ),
  },
];

const STATUS_LABEL: Record<Status, string> = {
  todo: "To do",
  done: "Done",
  problem: "Problem",
  skipped: "Skipped",
};

/** TOML for what the checklist found that differs from the profile, for the owner to copy. */
function suggestions(record: BringUpRecord, d: MachineProfile): string {
  const lines: string[] = [];
  const jog = record.steps.jog?.values ?? {};
  if (jog.x_points && jog.x_points !== d.x_points) lines.push(`x_points = "${jog.x_points}"`);
  if (jog.y_points && jog.y_points !== d.y_points) lines.push(`y_points = "${jog.y_points}"`);
  const down = record.steps.nozzles?.values.down_nozzle;
  const n1 = d.nozzles[0];
  if (down && n1 && !n1.label.toLowerCase().startsWith(String(down))) {
    lines.push(`# ${n1.id} is the ${down} nozzle: swap the nozzles' labels.`);
  }
  const groups = new Map<string, { x: number; y: number }[]>();
  for (const p of record.points) {
    const key = p.label.trim().split(/\s+/)[0]!.toLowerCase();
    groups.set(key, [...(groups.get(key) ?? []), p]);
  }
  for (const [key, points] of groups) {
    if (points.length < 2) continue;
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const r = (v: number) => Number(v.toFixed(2));
    lines.push(
      "",
      "[[keep_outs]]",
      `id = "${key.replace(/[^a-z0-9_-]/g, "_")}"`,
      `label = "${key[0]!.toUpperCase()}${key.slice(1)}"`,
      `area = { x_min = ${r(Math.min(...xs))}, y_min = ${r(Math.min(...ys))}, x_max = ${r(Math.max(...xs))}, y_max = ${r(Math.max(...ys))} }`,
    );
  }
  if (record.points.length) {
    lines.push("", "# Recorded points:");
    for (const p of record.points) lines.push(`#   ${p.label}: (${p.x.toFixed(3)}, ${p.y.toFixed(3)})`);
  }
  return lines.join("\n").trim();
}

/** The machine bring-up checklist: progress and findings are kept by the core. */
export function BringUpPanel({
  description,
  machine,
  ...actions
}: Actions & { description: MachineProfile; machine?: MachineState }) {
  const { status } = useCore();
  const [record, setRecord] = useState<BringUpRecord | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/bring-up").then(({ data }) => {
      if (data && !cancelled) setRecord(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status]);

  const save = (next: BringUpRecord) => {
    setRecord(next);
    void api.PUT("/api/v1/bring-up", { body: next }).then(({ error }) => {
      setSaveError(error ? "Could not save the checklist." : null);
    });
  };

  if (!record) return null;

  const stepRecord = (id: string): StepRecord =>
    record.steps[id] ?? { status: "todo", note: "", values: {}, updated: null };
  const updateStep = (id: string, change: Partial<StepRecord>) =>
    save({
      ...record,
      steps: {
        ...record.steps,
        [id]: { ...stepRecord(id), ...change, updated: new Date().toISOString() },
      },
    });

  const recordPoint = () => {
    const x = machine?.position.X;
    const y = machine?.position.Y;
    const camera = description.cameras.find((c) => c.mount === "head" && c.looking === "down");
    if (x === undefined || y === undefined || !label.trim()) return;
    save({
      ...record,
      points: [
        ...record.points,
        {
          label: label.trim(),
          x: Number((x + (camera?.position.x ?? 0)).toFixed(3)),
          y: Number((y + (camera?.position.y ?? 0)).toFixed(3)),
        },
      ],
    });
    setLabel("");
  };

  const done = STEPS.filter((s) => stepRecord(s.id).status === "done").length;
  const changes = suggestions(record, description);

  return (
    <Panel title={`Bring-up (${done}/${STEPS.length})`} collapseId="bring-up">
      {saveError && <p className="note note-danger">{saveError}</p>}
      <ol className="bringup-steps">
        {STEPS.map((step) => {
          const rec = stepRecord(step.id);
          const ctx: StepContext = {
            ...actions,
            description,
            machine,
            values: rec.values,
            setValue: (key, value) => updateStep(step.id, { values: { ...rec.values, [key]: value } }),
          };
          const check = step.check?.(ctx) ?? null;
          const expanded = open === step.id;
          const Chevron = expanded ? ChevronDown : ChevronRight;
          return (
            <li key={step.id} className={`bringup-step bringup-${rec.status}`}>
              <button
                type="button"
                className="bringup-title"
                aria-expanded={expanded}
                onClick={() => setOpen(expanded ? null : step.id)}
              >
                <Chevron size={14} aria-hidden />
                <span className="bringup-name">{step.title}</span>
                {check === true && <Check size={14} className="text-ok" aria-label="Checks out" />}
                {check === false && (
                  <CircleAlert size={14} className="text-warn" aria-label="Differs" />
                )}
                <span className={`chip bringup-chip-${rec.status}`}>{STATUS_LABEL[rec.status]}</span>
              </button>
              {expanded && (
                <div className="bringup-body">
                  {step.body(ctx)}
                  {step.id === "keep_outs" && (
                    <>
                      <div className="bringup-row">
                        <input
                          className="input"
                          placeholder="Label, such as: datum front left"
                          value={label}
                          onChange={(e) => setLabel(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && recordPoint()}
                        />
                        <button
                          type="button"
                          className="button"
                          disabled={!machine?.homed || !label.trim()}
                          onClick={recordPoint}
                        >
                          <MapPin size={16} aria-hidden />
                          Record
                        </button>
                      </div>
                      <ul className="bringup-points">
                        {record.points.map((p, i) => (
                          <li key={i} className="mono">
                            <span>
                              {p.label}: ({p.x.toFixed(2)}, {p.y.toFixed(2)})
                            </span>
                            <button
                              type="button"
                              className="icon-button"
                              aria-label={`Remove ${p.label}`}
                              onClick={() =>
                                save({ ...record, points: record.points.filter((_, j) => j !== i) })
                              }
                            >
                              <X size={12} aria-hidden />
                            </button>
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                  <textarea
                    className="input bringup-note"
                    placeholder="Notes"
                    defaultValue={rec.note}
                    onBlur={(e) => {
                      if (e.target.value !== rec.note) updateStep(step.id, { note: e.target.value });
                    }}
                  />
                  <div className="segmented">
                    {(["done", "problem", "skipped", "todo"] as Status[]).map((s) => (
                      <button
                        key={s}
                        type="button"
                        className={rec.status === s ? "segment active" : "segment"}
                        onClick={() => updateStep(step.id, { status: s })}
                      >
                        {STATUS_LABEL[s]}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ol>
      {changes && (
        <details className="bringup-changes">
          <summary>Suggested profile changes</summary>
          <pre className="mono">{changes}</pre>
          <button
            type="button"
            className="button"
            onClick={() => void navigator.clipboard?.writeText(changes)}
          >
            <Copy size={16} aria-hidden />
            Copy
          </button>
        </details>
      )}
    </Panel>
  );
}
