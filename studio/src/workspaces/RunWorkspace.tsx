import { Hand, Pause, Play, RefreshCw, SkipForward, Square, StepForward } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { hasControl, useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { useCameras } from "../api/useCameras";
import { useDescription } from "../api/useDescription";
import { useFeeders } from "../api/useFeeders";
import type {
  Job,
  JobLayout,
  JobPlan,
  JobSummary,
  PickCorrection,
  RunItem,
  RunState,
} from "../api/types";
import { CameraView } from "../components/CameraView";
import { JobOverlay } from "../components/job/JobOverlay";
import { MachineView } from "../components/machine/MachineView";
import { FixedFeederPanel } from "../components/FixedFeederPanel";
import { Field } from "../components/Form";
import { Panel } from "../components/Panel";
import { PickCorrectionSelect } from "../components/PickCorrection";
import { Placeholder } from "../components/Placeholder";

const STATUS_LABEL: Record<RunItem["status"], string> = {
  pending: "to do",
  placed: "placed",
  skipped: "skipped",
  failed: "failed",
  blocked: "blocked",
};

/** Run a saved job: check it against the machine and its feeders, then place it. */
export function RunWorkspace() {
  const core = useCore();
  const { status, machine, operations } = core;
  const description = useDescription();
  const cameras = useCameras();
  const [feeders, setFeeders] = useFeeders();
  const { run: act, pending, error } = useAction();
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [jobId, setJobId] = useState("");
  const [plan, setPlan] = useState<JobPlan | null>(null);
  const [layout, setLayout] = useState<JobLayout | null>(null);
  const [run, setRun] = useState<RunState | null>(null);
  const [correction, setCorrection] = useState<PickCorrection | null>(null);
  const [configured, setConfigured] = useState<PickCorrection>();
  const [skipBlocked, setSkipBlocked] = useState(false);
  const [record, setRecord] = useState(false);
  const [checking, setChecking] = useState(0);

  const controlledElsewhere = !!core.lease?.holder && !hasControl(core);
  const canMove =
    status === "open" &&
    !!machine?.motion_enabled &&
    !machine.busy &&
    !!machine.homed &&
    !controlledElsewhere &&
    !pending;
  const canSignal = status === "open" && !controlledElsewhere && !pending;

  // Fetch again whenever an operation finishes (a scan, a run pausing, ...).
  const finished = Object.values(operations).reduce(
    (latest, o) => (o.finished_at && o.finished_at > latest ? o.finished_at : latest),
    "",
  );
  const running = run?.status === "running";

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void Promise.all([
      api.GET("/api/v1/jobs"),
      api.GET("/api/v1/run"),
      api.GET("/api/v1/placement"),
    ]).then(([j, r, p]) => {
      if (cancelled) return;
      if (j.data) setJobs(j.data);
      if (p.data) setConfigured(p.data.pick_correction);
      if (r.data !== undefined) {
        setRun(r.data);
        if (r.data) setJobId((id) => id || r.data!.job_id);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [status, finished]);

  // While a run is going, follow it.
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      void api.GET("/api/v1/run").then(({ data }) => {
        if (data !== undefined) setRun(data);
      });
    }, 600);
    return () => window.clearInterval(timer);
  }, [running]);

  const chosen = jobId || jobs[0]?.id || "";

  // The plan (a dry run) for the chosen job, and its outline for the map.
  useEffect(() => {
    if (status !== "open" || !chosen) return;
    let cancelled = false;
    void api.POST("/api/v1/run/plan", { body: { job_id: chosen } }).then(({ data }) => {
      if (!cancelled) setPlan(data ?? null);
    });
    void api
      .GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: chosen } } })
      .then(async ({ data }: { data?: Job }) => {
        if (!data || cancelled) return;
        const result = await api.POST("/api/v1/jobs/layout", { body: data });
        if (!cancelled) setLayout(result.data ?? null);
      });
    return () => {
      cancelled = true;
    };
  }, [status, chosen, finished, checking]);

  const current = run && run.job_id === chosen ? run : null;
  const items = useMemo(() => current?.plan.items ?? plan?.items ?? [], [current, plan]);
  const shownPlan = current?.plan ?? plan;
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of items) c[i.status] = (c[i.status] ?? 0) + 1;
    return c;
  }, [items]);
  const todo = (counts.pending ?? 0) + (counts.placed ?? 0);
  const blocked = counts.blocked ?? 0;

  const refresh = async () => {
    const { data } = await api.GET("/api/v1/run");
    if (data !== undefined) setRun(data);
  };
  const start = async () => {
    const op = await act(() =>
      api.POST("/api/v1/run/start", {
        body: {
          job_id: chosen,
          skip_blocked: skipBlocked,
          pick_correction: correction,
          record,
        },
      }),
    );
    if (op) await refresh();
  };
  const signal = async (path: "/api/v1/run/pause" | "/api/v1/run/stop") => {
    const state = await act(() => api.POST(path));
    if (state) setRun(state);
  };
  const resume = async (path: "/api/v1/run/resume" | "/api/v1/run/step") => {
    const op = await act(() => api.POST(path));
    if (op) await refresh();
  };
  const removed = async (nozzle: string) => {
    const op = await act(() =>
      api.POST("/api/v1/placement/release", { body: { nozzle_id: nozzle } }),
    );
    if (op) await refresh();
  };
  const skip = async (item: RunItem, value: boolean) => {
    const state = await act(() =>
      api.PUT("/api/v1/run/items/{index}", {
        params: { path: { index: item.index } },
        body: { skip: value },
      }),
    );
    if (state) setRun(state);
  };

  const paused = current?.status === "paused";
  const startable = !current || current.status === "finished" || current.status === "stopped";
  const errors = shownPlan?.issues.filter((i) => i.severity === "error") ?? [];
  const cautions = shownPlan?.issues.filter((i) => i.severity === "warning") ?? [];
  const topCamera = description?.cameras.find((c) => c.looking === "down");

  return (
    <div className="workspace">
      <aside className="sidebar">
        <Panel title="Job">
          {jobs.length === 0 ? (
            <p className="text-muted">No saved jobs. Prepare one first.</p>
          ) : (
            <select
              className="input"
              value={chosen}
              disabled={running}
              onChange={(e) => setJobId(e.target.value)}
            >
              {jobs.map((j) => (
                <option key={j.id} value={j.id}>
                  {j.name}
                  {j.placed ? "" : " (not placed on the bed)"}
                </option>
              ))}
            </select>
          )}
          {shownPlan && (
            <p className="note text-muted">
              {todo} placement{todo === 1 ? "" : "s"}
              {blocked ? `, ${blocked} blocked` : ""}
              {counts.skipped ? `, ${counts.skipped} left out` : ""}; {shownPlan.fiducials}{" "}
              fiducial{shownPlan.fiducials === 1 ? "" : "s"} to find.
            </p>
          )}
          {current && (
            <div className="run-progress">
              <progress max={Math.max(todo, 1)} value={counts.placed ?? 0} />
              <span>
                {counts.placed ?? 0} / {todo}
              </span>
            </div>
          )}

          {startable && (
            <>
              <Field label="Keep picks centered with">
                <PickCorrectionSelect
                  value={correction}
                  onChange={setCorrection}
                  configured={configured}
                />
              </Field>
              <label className="check">
                <input
                  type="checkbox"
                  checked={record}
                  onChange={(e) => setRecord(e.target.checked)}
                />
                Record it, to send to PicnicPlace's developers
              </label>
              {blocked > 0 && (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={skipBlocked}
                    onChange={(e) => setSkipBlocked(e.target.checked)}
                  />
                  Place the rest, leaving out the {blocked} blocked
                </label>
              )}
            </>
          )}

          <div className="button-row">
            {startable && (
              <button
                type="button"
                className="button primary"
                disabled={!canMove || !chosen || !shownPlan?.runnable || (blocked > 0 && !skipBlocked)}
                onClick={() => void start()}
              >
                <Play size={16} aria-hidden />
                {current ? "Run again" : "Start"}
              </button>
            )}
            {running && (
              <button
                type="button"
                className="button"
                disabled={!canSignal || !!current?.pause_requested}
                onClick={() => void signal("/api/v1/run/pause")}
              >
                <Pause size={16} aria-hidden />
                Pause
              </button>
            )}
            {paused && (
              <>
                <button
                  type="button"
                  className="button primary"
                  disabled={!canMove}
                  onClick={() => void resume("/api/v1/run/resume")}
                >
                  <Play size={16} aria-hidden />
                  Resume
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={!canMove}
                  title="Place one part on each nozzle, then pause"
                  onClick={() => void resume("/api/v1/run/step")}
                >
                  <StepForward size={16} aria-hidden />
                  Step
                </button>
              </>
            )}
            {(running || paused) && (
              <button
                type="button"
                className="button"
                disabled={!canSignal || !!current?.stop_requested}
                onClick={() => void signal("/api/v1/run/stop")}
              >
                <Square size={16} aria-hidden />
                Stop
              </button>
            )}
            <button
              type="button"
              className="button button-icon"
              title="Check again"
              aria-label="Check again"
              disabled={running}
              onClick={() => setChecking((n) => n + 1)}
            >
              <RefreshCw size={16} aria-hidden />
            </button>
          </div>
          {!machine?.homed && status === "open" && (
            <p className="note text-muted">Home the machine (Machine tab) to run a job.</p>
          )}
          {current?.message && (
            <p className={`note ${current.error ? "text-danger" : "text-muted"}`}>
              {current.error ?? current.message}
            </p>
          )}
          {paused &&
            Object.entries(current.held).map(([nozzle, index]) => (
              <div key={nozzle} className="button-row">
                <span className="note text-muted">
                  {nozzle} holds {current.plan.items[index]?.ref ?? "a part"}.
                </span>
                <button
                  type="button"
                  className="button"
                  disabled={!canSignal}
                  title={
                    "You took the part off the nozzle by hand (or it is empty): turn its " +
                    "suction off and pick that placement again"
                  }
                  onClick={() => void removed(nozzle)}
                >
                  <Hand size={16} aria-hidden />
                  Part removed
                </button>
              </div>
            ))}
          {current?.fix && current.fix.fiducials.length > 0 && (
            <p className="note text-muted">
              Board found {Math.hypot(current.fix.dx, current.fix.dy).toFixed(2)} mm off, turned{" "}
              {current.fix.angle.toFixed(2)}°; fiducials within{" "}
              {current.fix.residual.toFixed(2)} mm of the fit.
            </p>
          )}
          {error && <p className="note note-danger">{error}</p>}
        </Panel>

        {shownPlan && (errors.length > 0 || cautions.length > 0) && (
          <Panel title="Checks" collapseId="run-checks">
            {errors.map((i, n) => (
              <p key={n} className="note note-danger">
                {i.copy_id ? `Board ${i.copy_id}: ` : ""}
                {i.message}
              </p>
            ))}
            {cautions.map((i, n) => (
              <p key={n} className="note note-warn">
                {i.message}
              </p>
            ))}
          </Panel>
        )}

        {shownPlan && shownPlan.parts.length > 0 && (
          <Panel title="Parts and feeders" collapseId="run-parts">
            <ul className="item-list">
              {shownPlan.parts.map((p) => (
                <li key={p.part_id ?? "-"} className="run-row">
                  <span className="item-id">{p.part_id ?? "no part number"}</span>
                  <span className="text-muted">×{p.count}</span>
                  <span className={p.problem ? "text-danger run-where" : "text-muted run-where"}>
                    {p.slot_ids.length ? sources(p.slot_ids) : (p.problem ?? "no feeder")}
                  </span>
                </li>
              ))}
            </ul>
          </Panel>
        )}

        {feeders && feeders.fixed.length > 0 && (
          <FixedFeederPanel fixed={feeders.fixed} onChanged={setFeeders} />
        )}

        {items.length > 0 && (
          <Panel title="Placements" collapseId="run-items">
            <ul className="item-list">
              {items.map((i) => (
                <li key={i.index} className="run-row" title={i.note || undefined}>
                  <span className={`run-dot run-${i.status}`} aria-hidden />
                  <span className="item-id">
                    {i.ref}
                    {i.copy_id ? <span className="text-muted"> ·{i.copy_id}</span> : null}
                  </span>
                  <span className="text-muted run-where">
                    {STATUS_LABEL[i.status]}
                    {i.nozzle ? ` by ${i.nozzle}` : ""}
                    {i.note && i.status !== "placed" ? `: ${i.note}` : ""}
                  </span>
                  {current &&
                    (i.status === "pending" ||
                      i.status === "failed" ||
                      (i.status === "skipped" && i.note === "skipped by the operator")) && (
                      <button
                        type="button"
                        className="icon-button"
                        disabled={!canSignal || Object.values(current.held).includes(i.index)}
                        title={i.status === "skipped" ? `Place ${i.ref} after all` : `Skip ${i.ref}`}
                        onClick={() => void skip(i, i.status !== "skipped")}
                      >
                        {i.status === "skipped" ? (
                          <RefreshCw size={14} aria-label={`Place ${i.ref} after all`} />
                        ) : (
                          <SkipForward size={14} aria-label={`Skip ${i.ref}`} />
                        )}
                      </button>
                    )}
                </li>
              ))}
            </ul>
          </Panel>
        )}

        {topCamera && (
          <CameraView
            id={topCamera.id}
            title={topCamera.label}
            info={cameras?.find((c) => c.id === topCamera.id)}
          />
        )}
      </aside>

      <div className="canvas machine-canvas">
        {description ? (
          <MachineView
            description={description}
            machine={machine}
            overlay={
              <>
                {layout && <JobOverlay layout={layout} />}
                <RunOverlay items={items} />
              </>
            }
            fixedFeeders={feeders?.fixed}
          />
        ) : (
          <Placeholder
            icon={Play}
            title="Run"
            description="Waiting for the machine description from the core."
          />
        )}
      </div>
    </div>
  );
}

/** "slots 3, 7 and T1": feeder slots by number, strips and trays by ID. */
function sources(ids: string[]): string {
  const slots = ids.filter((id) => /^\d+$/.test(id));
  const fixed = ids.filter((id) => !/^\d+$/.test(id));
  const parts = [
    ...(slots.length ? [`slot${slots.length > 1 ? "s" : ""} ${slots.join(", ")}`] : []),
    ...fixed,
  ];
  return parts.join(", ");
}

/** Each placement on the map, colored by how far the run has got with it. */
function RunOverlay({ items }: { items: RunItem[] }) {
  return (
    <g className="run-overlay">
      {items.map((i) => (
        <circle key={i.index} className={`run-mark run-${i.status}`} cx={i.x} cy={-i.y} r={1.1}>
          <title>{`${i.ref} ${i.part_id ?? ""}: ${STATUS_LABEL[i.status]}${i.note ? ` (${i.note})` : ""}`}</title>
        </circle>
      ))}
    </g>
  );
}
