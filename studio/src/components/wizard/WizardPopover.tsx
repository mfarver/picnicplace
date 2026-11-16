import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  CircleSlash,
  Loader2,
  Minus,
  Pause,
  Play,
  RotateCw,
  SkipForward,
  Square,
  XCircle,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useCore } from "../../api/core";
import { useAction } from "../../api/useAction";
import { useSetup } from "../../api/useSetup";
import { OPEN_WIZARD, useWizard, wizardChanged } from "../../api/useWizard";
import type {
  MachineProfile,
  MachineState,
  SetupState,
  WizardPage,
  WizardState,
} from "../../api/types";
import { BringUpSteps } from "../BringUpSteps";
import { MachineLocations } from "../MachineLocations";
import { NozzleTips } from "../NozzleTips";
import { CamerasPage, ConnectPage, MachinePage } from "./SetupPages";
import { STOP_TEXT, stageMessage } from "./wizardPages";

type Stop = WizardPage["stop"];
type RunStep = NonNullable<WizardState["run"]>["steps"][number];

const PAGE_STATUS: Record<WizardPage["status"], { label: string; icon: ReactNode }> = {
  done: { label: "Done", icon: <CheckCircle2 size={14} aria-hidden /> },
  todo: { label: "To do", icon: <Circle size={14} aria-hidden /> },
  stale: { label: "Out of date", icon: <AlertTriangle size={14} aria-hidden /> },
  skipped: { label: "Skipped", icon: <SkipForward size={14} aria-hidden /> },
  not_needed: { label: "Not needed", icon: <Minus size={14} aria-hidden /> },
  not_built: { label: "Not built yet", icon: <CircleSlash size={14} aria-hidden /> },
};

const RUN_STATUS: Record<RunStep["status"], { label: string; icon: ReactNode }> = {
  waiting: { label: "Waiting", icon: <Circle size={14} aria-hidden /> },
  running: { label: "Running", icon: <Loader2 size={14} className="spin" aria-hidden /> },
  done: { label: "Done", icon: <CheckCircle2 size={14} aria-hidden /> },
  failed: { label: "Failed", icon: <XCircle size={14} aria-hidden /> },
  skipped: { label: "Skipped", icon: <SkipForward size={14} aria-hidden /> },
  stopped: { label: "Stopped", icon: <Square size={14} aria-hidden /> },
};

interface Props {
  description: MachineProfile;
  machine?: MachineState;
  /** This client may move the machine. */
  canMove: boolean;
  /** Homed, and this client may change the machine's locations. */
  canSet: boolean;
  /** This client holds control, or may take it. */
  canConfirm: boolean;
  onHome: () => void;
  /** Ask that the nozzles be level, then run `go` (homing elsewhere than `onHome`). */
  onAskHome: (go: () => void) => void;
  onJog: (axis: string, distance: number) => void;
}

/** Pages a stop is made of that still need something. */
function open(pages: WizardPage[]): WizardPage[] {
  return pages.filter((p) => p.status === "stale" || (p.status === "todo" && !p.optional));
}

/**
 * The setup wizard, over the machine map (spec §7.7, §8.2): three stops, each asking the
 * owner for the one thing the machine cannot do itself, then one Start that runs every
 * step the machine can, showing each as it runs and what it found.
 */
export function WizardPopover(props: Props) {
  const [state, setState] = useWizard();
  const [setup, setSetup] = useSetup();
  const [expanded, setExpanded] = useState<boolean | null>(null);
  // Open when the Studio starts on a machine still to calibrate; after that, only the
  // owner (or Continue calibration) opens or closes it.
  if (state && expanded === null) setExpanded(state.stage !== "calibrated");
  const [shown, setShown] = useState<Stop | null>(null);

  // The banner's Continue calibration opens it, at the next stop.
  useEffect(() => {
    const show = () => {
      setExpanded(true);
      setShown(null);
    };
    window.addEventListener(OPEN_WIZARD, show);
    return () => window.removeEventListener(OPEN_WIZARD, show);
  }, []);

  // While a stop runs, follow its steps.
  const running = !!state?.run?.running;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      void api.GET("/api/v1/wizard").then(({ data }) => data && setState(data));
    }, 1000);
    return () => clearInterval(timer);
  }, [running, setState]);

  if (!state) return null;
  const isOpen = expanded ?? false;
  const current: Stop = shown ?? (running ? (state.run!.stop as Stop) : (state.next_stop ?? 3));
  const onSetup = (s: SetupState) => {
    setSetup(s);
    wizardChanged();
  };

  return (
    <section
      className={`head-inset wizard-popover${isOpen ? "" : " head-inset-collapsed"}`}
      aria-label="Setup wizard"
    >
      <header className="head-inset-header">
        <span className="panel-title-text">Setup wizard</span>
        <button
          type="button"
          className="icon-button icon-button-sm"
          onClick={() => setExpanded(!isOpen)}
          title={isOpen ? "Collapse" : "Expand"}
          aria-expanded={isOpen}
        >
          {isOpen ? <ChevronUp size={14} aria-hidden /> : <ChevronDown size={14} aria-hidden />}
        </button>
      </header>
      {isOpen && (
        <div className="head-inset-body wizard-body">
          <p className={`note${state.stage === "calibrated" ? " text-muted" : " note-warn"}`}>
            {stageMessage(state)}
          </p>
          {([0, 1, 2, 3] as Stop[]).map((stop) => {
            const pages = state.pages.filter(
              (p) => p.stop === stop && p.id !== "welcome" && p.id !== "done",
            );
            const needs = open(pages);
            // Choosing the machine shows only when there is something to choose.
            if (stop === 0 && needs.length === 0) return null;
            const active = stop === current;
            return (
              <div key={stop} className={`wizard-stop${active ? " active" : ""}`}>
                <button
                  type="button"
                  className="wizard-stop-head"
                  aria-expanded={active}
                  onClick={() => setShown(stop)}
                >
                  <span className="wizard-stop-number">{stop === 0 ? "•" : stop}</span>
                  <span className="wizard-stop-title">{STOP_TEXT[stop].title}</span>
                  {needs.length === 0 ? (
                    <CheckCircle2 size={14} className="text-ok" aria-label="Done" />
                  ) : (
                    <Circle size={14} className="text-faint" aria-label="To do" />
                  )}
                </button>
                {active && (
                  <StopBody
                    {...props}
                    stop={stop}
                    pages={pages}
                    state={state}
                    setup={setup}
                    onSetup={onSetup}
                    onState={setState}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function StopBody({
  stop,
  pages,
  state,
  setup,
  onSetup,
  onState,
  description,
  machine,
  canMove,
  canSet,
  canConfirm,
  onHome,
  onAskHome,
  onJog,
}: Props & {
  stop: Stop;
  pages: WizardPage[];
  state: WizardState;
  setup: SetupState | null;
  onSetup: (s: SetupState) => void;
  onState: (s: WizardState) => void;
}) {
  const text = STOP_TEXT[stop];
  const { run, pending, error } = useAction();
  const { operations } = useCore();
  const [runoutOpId, setRunoutOpId] = useState<string | null>(null);
  const runoutOp = runoutOpId ? operations[runoutOpId] : undefined;
  const [handChecks, setHandChecks] = useState(false);
  const live = state.run && state.run.stop === stop ? state.run : null;
  const running = !!state.run?.running;
  const paused = !!machine?.paused;
  const ids = new Set(pages.map((p) => p.id));
  const setupProps = setup ? { setup, onSetup, canChange: canConfirm } : null;

  const start = async () => {
    const op = await run(() =>
      api.POST("/api/v1/wizard/run", {
        body: { stop: stop as 1 | 2, redo: open(pages).length === 0 },
      }),
    );
    if (op) {
      const { data } = await api.GET("/api/v1/wizard");
      if (data) onState(data);
    }
  };
  // Stop 1 homes first, unless the machine is homed: ask that the nozzles be level.
  const begin = () =>
    stop === 1 && !machine?.homed ? onAskHome(() => void start()) : void start();
  const confirm = async (pageId: "discard_park" | "strips_trays", status: "done" | "skipped") => {
    const updated = await run(() =>
      api.PUT("/api/v1/wizard/pages/{page_id}", {
        params: { path: { page_id: pageId } },
        body: { status },
      }),
    );
    if (updated) {
      onState(updated);
      wizardChanged();
    }
  };
  // Measure every nozzle's runout again: after reseating a tip, say.
  const measureRunout = async () => {
    const op = await run(() =>
      api.POST("/api/v1/machine/nozzle-runout", {
        body: { nozzle_ids: description.nozzles.map((n) => n.id) },
      }),
    );
    if (op) setRunoutOpId(op.id);
  };
  const hasDiscard = description.locations.some((l) => l.kind === "discard");
  const tipsTodo = pages.some((p) => p.id === "nozzle_tips" && p.status === "todo");
  const discard = pages.find((p) => p.id === "discard_park");

  return (
    <div className="wizard-stop-body">
      <p>{text.ask}</p>
      {text.runs && <p className="text-muted">{text.runs}</p>}

      {stop === 0 && setupProps && (
        <>
          {ids.has("machine") && <MachinePage {...setupProps} />}
          {pages.some((p) => p.id === "connect" && p.status === "todo") && (
            <ConnectPage {...setupProps} />
          )}
          {pages.some((p) => p.id === "cameras" && p.status === "todo") && (
            <CamerasPage {...setupProps} description={description} />
          )}
        </>
      )}

      {(stop === 1 || stop === 2) && (
        <div className="button-row">
          <button
            type="button"
            className="button primary"
            disabled={!canMove || running || pending}
            onClick={begin}
          >
            <Play size={16} aria-hidden />
            {open(pages).length === 0 ? "Run again" : "Start"}
          </button>
          {running && !paused && (
            <button
              type="button"
              className="button"
              onClick={() => void run(() => api.POST("/api/v1/machine/pause"))}
            >
              <Pause size={16} aria-hidden />
              Pause
            </button>
          )}
          {running && paused && (
            <button
              type="button"
              className="button"
              disabled={!canConfirm}
              onClick={() => void run(() => api.POST("/api/v1/machine/resume"))}
            >
              <Play size={16} aria-hidden />
              Resume
            </button>
          )}
          {running && (
            <button
              type="button"
              className="button"
              onClick={() => void run(() => api.POST("/api/v1/machine/stop"))}
            >
              <Square size={16} aria-hidden />
              Stop
            </button>
          )}
        </div>
      )}
      {running && (
        <p className="text-faint">
          {paused
            ? "Paused: the machine waits before its next move. Resume carries on; Stop ends " +
              "the run."
            : "Pause and Stop act when the move under way finishes; the machine stays homed. " +
              "In an emergency, switch the machine off."}
        </p>
      )}

      <ol className="calibration-list wizard-steps">
        {live
          ? live.steps.map((step) => (
              <li key={step.id} className={`calibration-line wizard-run-${step.status}`}>
                <div className="calibration-title">
                  <span className="calibration-name">{step.title}</span>
                  <span className="calibration-chip">
                    {RUN_STATUS[step.status].icon}
                    {RUN_STATUS[step.status].label}
                  </span>
                </div>
                {step.message && <p className="calibration-body">{step.message}</p>}
              </li>
            ))
          : pages
              .filter((p) => p.status !== "not_needed" && p.status !== "not_built")
              .map((p) => (
                <li key={p.id} className={`calibration-line wizard-${p.status}`}>
                  <div className="calibration-title">
                    <span className="calibration-name">
                      {p.title}
                      {p.optional && <span className="text-faint"> (optional)</span>}
                    </span>
                    <span className="calibration-chip">
                      {PAGE_STATUS[p.status].icon}
                      {PAGE_STATUS[p.status].label}
                    </span>
                  </div>
                  <p className="calibration-body">
                    {p.detail}
                    {p.stale && <span className="text-warn"> Out of date: {p.stale}.</span>}
                    {p.warning && <span className="text-warn"> {p.warning}.</span>}
                  </p>
                </li>
              ))}
      </ol>
      {live && !running && (
        <details className="wizard-details">
          <summary>Where each page stands</summary>
          <ul>
            {pages.map((p) => (
              <li key={p.id}>
                {p.title}: {PAGE_STATUS[p.status].label.toLowerCase()}. {p.detail}
              </li>
            ))}
          </ul>
        </details>
      )}

      {stop === 2 && !running && (
        <>
          <div className="button-row">
            <button
              type="button"
              className="button"
              disabled={!canMove || !machine?.homed || pending || runoutOp?.status === "running"}
              title="Turn each nozzle's tip over the bottom camera again"
              onClick={() => void measureRunout()}
            >
              <RotateCw size={16} aria-hidden />
              Measure runout
            </button>
          </div>
          <p className="text-faint">
            Runout is measured by itself after each tip change. Measure it again after
            pushing a tip back on.
          </p>
          {runoutOp?.status === "failed" && (
            <p className="note note-danger">{runoutOp.error}</p>
          )}
        </>
      )}

      {stop === 1 && tipsTodo && (
        <>
          <p className="text-muted">
            If the bottom camera could not name a tip, say which it is:
          </p>
          <NozzleTips
            description={description}
            canSet={canConfirm}
            canMove={canMove && !!machine?.homed}
          />
        </>
      )}
      {stop === 1 && (
        <details
          className="wizard-details"
          open={handChecks}
          onToggle={(e) => setHandChecks((e.target as HTMLDetailsElement).open)}
        >
          <summary>Check by hand (optional)</summary>
          {handChecks && (
            <BringUpSteps
              description={description}
              machine={machine}
              steps={["slow", "home", "jog", "nozzles", "outputs", "stop", "camera", "keep_outs"]}
              canMove={canMove}
              onHome={onHome}
              onJog={onJog}
            />
          )}
        </details>
      )}

      {stop === 3 && (
        <>
          <MachineLocations description={description} canSet={canSet} />
          {discard && discard.status !== "done" && (
            <button
              type="button"
              className="button primary"
              disabled={!canConfirm || pending || !hasDiscard}
              onClick={() => void confirm("discard_park", "done")}
              title={hasDiscard ? undefined : "Set the discard spot first"}
            >
              <Check size={16} aria-hidden />
              They are right
            </button>
          )}
          <p className="text-faint">
            Strips and trays on the bed are taught from the Feeders workspace, when you need
            them.
          </p>
          {state.stage === "calibrated" && (
            <BringUpSteps
              description={description}
              machine={machine}
              steps={["first_part"]}
              canMove={canMove}
              onHome={onHome}
              onJog={onJog}
            />
          )}
        </>
      )}
      {error && <p className="note note-danger">{error}</p>}
    </div>
  );
}
