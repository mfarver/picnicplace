import {
  Crosshair,
  Eye,
  Hand,
  LocateFixed,
  Pause,
  Play,
  RefreshCw,
  Square,
  StepForward,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { hasControl, useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { useCameras } from "../api/useCameras";
import { useDescription } from "../api/useDescription";
import { useFeeders } from "../api/useFeeders";
import { useRun } from "../api/useRun";
import type {
  BoardCheck,
  BoardFix,
  JobPlan,
  JobSummary,
  PickCorrection,
  RunItem,
  RunState,
} from "../api/types";
import { CameraView } from "../components/CameraView";
import { FixedFeederPanel } from "../components/FixedFeederPanel";
import { Field } from "../components/Form";
import { Panel } from "../components/Panel";
import { PickCorrectionSelect } from "../components/PickCorrection";
import { RunSteps } from "../components/RunSteps";
import { Placeholder } from "../components/Placeholder";
import { useFootprint } from "../useFootprint";

/**
 * Run a saved job: check it against the machine and its feeders, then place it. This is
 * the operator's view, beside the machine: the job's steps fill it. Someone watching from
 * elsewhere uses the Machine workspace, which shows the map with the steps under it.
 */
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
  const [run, setRun, refresh] = useRun();
  const [correction, setCorrection] = useState<PickCorrection | null>(null);
  const [configured, setConfigured] = useState<PickCorrection>();
  const [skipBlocked, setSkipBlocked] = useState(false);
  const [record, setRecord] = useState(false);
  const [stepMode, setStepMode] = useState(false);
  const [checkPlaced, setCheckPlaced] = useState<boolean | null>(null);
  const [boardCheck, setBoardCheck] = useState<BoardCheck | null>(null);
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
      api.GET("/api/v1/placement"),
      api.GET("/api/v1/run/board-check"),
    ]).then(([j, p, b]) => {
      if (cancelled) return;
      if (j.data) setJobs(j.data);
      if (b.data !== undefined) setBoardCheck(b.data);
      if (p.data) setConfigured(p.data.pick_correction);
    });
    return () => {
      cancelled = true;
    };
  }, [status, finished]);

  // The job that was run last, unless another one has been chosen.
  const chosen = jobId || run?.job_id || jobs[0]?.id || "";

  // The plan (a dry run) for the chosen job.
  useEffect(() => {
    if (status !== "open" || !chosen) return;
    let cancelled = false;
    void api.POST("/api/v1/run/plan", { body: { job_id: chosen } }).then(({ data }) => {
      if (!cancelled) setPlan(data ?? null);
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

  const start = async () => {
    const op = await act(() =>
      api.POST("/api/v1/run/start", {
        body: {
          job_id: chosen,
          revision: plan?.revision ?? undefined,
          skip_blocked: skipBlocked,
          pick_correction: correction,
          record,
          step: stepMode,
          check_placements: checkPlaced,
        },
      }),
    );
    if (op) await refresh();
  };
  const checkBoard = () =>
    void act(() =>
      api.POST("/api/v1/run/check-board", {
        body: { job_id: chosen, revision: plan?.revision ?? undefined },
      }),
    );
  const goTo = (x: number, y: number) =>
    void act(() => api.POST("/api/v1/machine/move", { body: { position: { X: x, Y: y } } }));
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
  // Why the run paused, kept in view however far the list is scrolled, until hidden.
  const [hiddenError, setHiddenError] = useState<string | null>(null);
  const alert = paused && current?.error && current.error !== hiddenError ? current.error : null;
  const pausedAt = current?.current?.index;
  const pausedItem = pausedAt !== undefined && pausedAt !== null ? items[pausedAt] : undefined;
  // Paused on a pick: the feeder it picks from is what to look at, not the placement.
  const pausedPick = current?.current?.action === "picking" ? pausedItem?.slot_id : null;
  const pickAt = (() => {
    if (!pausedPick || !feeders) return null;
    const slot = feeders.slots.find((x) => x.slot.id === pausedPick);
    if (slot) return { label: slot.slot.label, at: slot.pick };
    const fixed = feeders.fixed.find((f) => f.id === pausedPick);
    return fixed?.next ? { label: fixed.id, at: fixed.next } : null;
  })();
  const lookAtPick = () => {
    if (!pickAt || !topCamera) return;
    setLooking(null);
    const target = { X: pickAt.at.x - topCamera.position.x, Y: pickAt.at.y - topCamera.position.y };
    void act(() => api.POST("/api/v1/machine/move", { body: { position: target } }));
  };
  const startable = !current || current.status === "finished" || current.status === "stopped";
  // While paused, the fiducials the run found, to look at before going on.
  const pausedFix = paused && current?.fix?.fiducials.length ? current.fix : null;
  const errors = shownPlan?.issues.filter((i) => i.severity === "error") ?? [];
  const cautions = shownPlan?.issues.filter((i) => i.severity === "warning") ?? [];
  const topCamera = description?.cameras.find((c) => c.looking === "down");
  // Where the top camera is now, to draw a placement's footprint on its picture.
  const topAt =
    topCamera && machine?.position.X !== undefined && machine.position.Y !== undefined
      ? {
          x: machine.position.X + topCamera.position.x,
          y: machine.position.Y + topCamera.position.y,
        }
      : undefined;

  // The placement being looked at with the top camera, its footprint drawn over it: where
  // the job's place puts it, before the run's fiducial fit.
  const [looking, setLooking] = useState<{ job: string; item: RunItem } | null>(null);
  const lookingItem = looking && looking.job === chosen ? looking.item : null;
  const lookFootprint = useFootprint(lookingItem?.footprint_id);
  const look = (item: RunItem) => {
    if (lookingItem?.index === item.index) {
      setLooking(null);
      return;
    }
    setLooking({ job: chosen, item });
    if (!topCamera) return;
    const target = { X: item.x - topCamera.position.x, Y: item.y - topCamera.position.y };
    void act(() => api.POST("/api/v1/machine/move", { body: { position: target } }));
  };

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
              {jobs.map((j) => {
                const latest = j.revisions.at(-1);
                return (
                  <option key={j.id} value={j.id}>
                    {j.name}
                    {latest ? ` · r${latest.number}` : " (not published)"}
                    {j.placed ? "" : " (not placed on the bed)"}
                  </option>
                );
              })}
            </select>
          )}
          {shownPlan && (
            <p className="note text-muted">
              {shownPlan.revision ? `Revision ${shownPlan.revision}: ` : ""}
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
              <label className="check" title="Then Step places one part on each nozzle at a time">
                <input
                  type="checkbox"
                  checked={stepMode}
                  onChange={(e) => setStepMode(e.target.checked)}
                />
                Start in step mode: pause once the board is found, before the first part
              </label>
              <label
                className="check"
                title={
                  "Look at each two-terminal chip with the top camera once placed, and pause " +
                  "if it landed off its target, turned, or is not there"
                }
              >
                <input
                  type="checkbox"
                  checked={checkPlaced ?? false}
                  onChange={(e) => setCheckPlaced(e.target.checked)}
                />
                Check each part with the top camera once placed
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
            {startable && (
              <button
                type="button"
                className="button"
                disabled={!canMove || !chosen || !shownPlan?.fiducials}
                title="Find the board's fiducials and show how it sits, placing nothing"
                onClick={checkBoard}
              >
                <Crosshair size={16} aria-hidden />
                Check fiducials
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
          {current?.fix && current.fix.fiducials.length > 0 && (
            <p className="note text-muted">
              Board found {Math.hypot(current.fix.dx, current.fix.dy).toFixed(2)} mm off, turned{" "}
              {current.fix.angle.toFixed(2)}°; fiducials within{" "}
              {current.fix.residual.toFixed(2)} mm of the fit.
            </p>
          )}
          {error && <p className="note note-danger">{error}</p>}
        </Panel>

        {pausedFix ? (
          <Panel title="Fiducials" collapseId="run-fiducials">
            <FiducialList fix={pausedFix} canMove={canMove} onGoTo={goTo} />
          </Panel>
        ) : (
          startable &&
          boardCheck &&
          boardCheck.job_id === chosen && (
            <Panel title="Fiducial check" collapseId="run-fiducials">
              <p className={`note ${boardCheck.ok ? "text-ok" : "text-danger"}`}>
                {boardCheck.ok ? "Good: " : ""}
                {boardCheck.message}
              </p>
              {boardCheck.fix && (
                <FiducialList fix={boardCheck.fix} canMove={canMove} onGoTo={goTo} />
              )}
            </Panel>
          )
        )}

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

        {topCamera && (
          <CameraView
            id={topCamera.id}
            title={topCamera.label}
            info={cameras?.find((c) => c.id === topCamera.id)}
            fieldOfView={topCamera.field_of_view.x}
            footprints={
              lookingItem && lookFootprint
                ? [
                    {
                      footprint: lookFootprint,
                      x: lookingItem.x,
                      y: lookingItem.y,
                      angle: lookingItem.rotation,
                      title: `${lookingItem.ref}: where the job puts it, pin 1 in red`,
                    },
                  ]
                : undefined
            }
            at={topAt}
            orientation={description}
          />
        )}
      </aside>

      <div className="canvas run-canvas">
        {shownPlan ? (
          <div className="run-main">
            <header className="run-head">
              <h1>
                <span className="text-muted">Job:</span> {shownPlan.name}
              </h1>
              {current ? (
                <span className="run-count">
                  {counts.placed ?? 0} of {todo} placed
                </span>
              ) : (
                <span className="text-muted">Not started</span>
              )}
            </header>
            {current?.message && (
              <p className={`run-message ${current.error ? "note note-danger" : "text-muted"}`}>
                {current.error ?? current.message}
              </p>
            )}
            {alert && (
              <div className="run-alert" role="alertdialog" aria-label="Why the run paused">
                <p className="run-alert-title">
                  <TriangleAlert size={18} aria-hidden />
                  Paused
                </p>
                <p className="run-alert-text">{alert}</p>
                <div className="button-row">
                  <button
                    type="button"
                    className="button primary"
                    disabled={!canMove}
                    onClick={() => void resume("/api/v1/run/resume")}
                  >
                    <Play size={16} aria-hidden />
                    Resume
                  </button>
                  {pickAt ? (
                    <button
                      type="button"
                      className="button"
                      disabled={!canMove || !topCamera}
                      title="Move the top camera over the pocket it picks from"
                      onClick={lookAtPick}
                    >
                      <Eye size={16} aria-hidden />
                      Look at {pickAt.label}
                    </button>
                  ) : (
                    pausedItem && (
                      <button
                        type="button"
                        className="button"
                        disabled={!canMove || !topCamera}
                        title="Move the top camera over it, its footprint drawn on the picture"
                        onClick={() => look(pausedItem)}
                      >
                        <Eye size={16} aria-hidden />
                        Look at {pausedItem.ref}
                      </button>
                    )
                  )}
                  <button
                    type="button"
                    className="button"
                    disabled={!canSignal}
                    title="End the run here; what is placed stays placed"
                    onClick={() => void signal("/api/v1/run/stop")}
                  >
                    <Square size={16} aria-hidden />
                    Stop
                  </button>
                  <button type="button" className="button" onClick={() => setHiddenError(alert)}>
                    Hide
                  </button>
                </div>
              </div>
            )}
            {current && current.stats.picks > 0 && <RunStatsLine stats={current.stats} />}
            {paused &&
              Object.entries(current.held).map(([nozzle, index]) => (
                <div key={nozzle} className="button-row run-held">
                  <span>
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
            <RunSteps
              items={items}
              run={current}
              fiducials={shownPlan.fiducials}
              hasBus={!!feeders?.bus}
              large
              canSignal={canSignal}
              onSkip={(item, value) => void skip(item, value)}
              onLook={look}
              looking={lookingItem?.index ?? null}
              canLook={canMove && !!topCamera}
            />
          </div>
        ) : (
          <Placeholder
            icon={Play}
            title="Run"
            description={jobs.length ? "Checking the job…" : "No saved jobs. Prepare one first."}
          />
        )}
      </div>
    </div>
  );
}

/** Each fiducial found: how far from where the job puts it, and a button to look at it. */
function FiducialList({
  fix,
  canMove,
  onGoTo,
}: {
  fix: BoardFix;
  canMove: boolean;
  onGoTo: (x: number, y: number) => void;
}) {
  return (
    <ul className="item-list">
      {fix.fiducials.map((f) => (
        <li key={`${f.ref}-${f.copy_id ?? ""}`} className="run-row">
          <span className="item-id">
            {f.ref}
            {f.copy_id ? <span className="text-muted"> ·{f.copy_id}</span> : null}
          </span>
          <span className="text-muted run-where" title="From where the job puts it">
            {Math.hypot(f.found.x - f.expected.x, f.found.y - f.expected.y).toFixed(2)} mm off
          </span>
          <button
            type="button"
            className="icon-button"
            disabled={!canMove}
            title={`Move the camera over ${f.ref}`}
            aria-label={`Go to ${f.ref}`}
            onClick={() => onGoTo(f.found.x, f.found.y)}
          >
            <LocateFixed size={14} aria-hidden />
          </button>
        </li>
      ))}
    </ul>
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

/** How the run went: picks, finding parts on the bottom camera, and placements checked. */
function RunStatsLine({ stats }: { stats: RunState["stats"] }) {
  const parts = [
    `${stats.picks} picks` + (stats.empty_picks ? `, ${stats.empty_picks} came up empty` : ""),
    `${stats.alignments} found by the bottom camera` +
      (stats.failed_alignments ? `, ${stats.failed_alignments} not found (discarded)` : ""),
  ];
  if (stats.checked) {
    parts.push(
      `${stats.checked} placements checked` +
        (stats.bad_placements ? `, ${stats.bad_placements} off target` : ", all on target") +
        (stats.unsure ? `, ${stats.unsure} could not be made out` : ""),
    );
  }
  const trouble = stats.empty_picks + stats.failed_alignments + stats.bad_placements > 0;
  return (
    <p className={`run-stats${trouble ? " text-warn" : " text-muted"}`}>{parts.join(" · ")}</p>
  );
}
