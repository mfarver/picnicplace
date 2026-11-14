import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  Circle,
  CircleSlash,
  LogOut,
  Minus,
  Play,
  SkipForward,
  Square,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useAction } from "../../api/useAction";
import { OPEN_WIZARD, useWizard } from "../../api/useWizard";
import type { MachineProfile, Operation, WizardPage, WizardState } from "../../api/types";
import { MachineLocations } from "../MachineLocations";
import { NozzleTips } from "../NozzleTips";
import { Panel } from "../Panel";
import { PAGE_TEXT, stageMessage } from "./wizardPages";

type Action = NonNullable<WizardPage["action"]>;

const STATUS: Record<WizardPage["status"], { label: string; icon: ReactNode }> = {
  done: { label: "Done", icon: <CheckCircle2 size={14} aria-hidden /> },
  todo: { label: "To do", icon: <Circle size={14} aria-hidden /> },
  stale: { label: "Out of date", icon: <AlertTriangle size={14} aria-hidden /> },
  skipped: { label: "Skipped", icon: <SkipForward size={14} aria-hidden /> },
  not_needed: { label: "Not needed", icon: <Minus size={14} aria-hidden /> },
  not_built: { label: "Not built yet", icon: <CircleSlash size={14} aria-hidden /> },
};

/** The operation each measuring page's action starts, to follow it while it runs. */
const ACTION_KIND: Record<Action, string> = {
  home: "home",
  visual_home: "visual_home",
  camera_scale: "camera_scale",
  bottom_camera: "bottom_camera",
  nozzle_offsets: "nozzle_offsets",
  calibrate_feeders: "rail_calibrate",
};

interface Props {
  description: MachineProfile;
  operations: Record<string, Operation>;
  /** Homed, and this client may move the machine. */
  canRun: boolean;
  /** Homed, and this client may change the machine's locations. */
  canSet: boolean;
  /** This client holds control, or may take it: it may confirm pages and fit tips. */
  canConfirm: boolean;
  onAction: (action: Action) => void;
  onStop: () => void;
}

/**
 * The setup wizard (spec §7.7, §8.2): every page from the machine to a calibrated
 * machine, where each stands, and Continue to the next. A page says what it learns, what
 * to do before it, and what will happen; then it runs, and shows what it found.
 */
export function WizardPanel(props: Props) {
  const { description, operations } = props;
  const [state, setState] = useWizard();
  const [view, setView] = useState<WizardPage["id"] | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  // The banner's Continue calibration, from any workspace, opens a page here.
  useEffect(() => {
    const open = (e: Event) => {
      const page = (e as CustomEvent<WizardPage["id"] | null>).detail;
      setView(page ?? "welcome");
      panel.current?.scrollIntoView({ block: "start" });
    };
    window.addEventListener(OPEN_WIZARD, open);
    return () => window.removeEventListener(OPEN_WIZARD, open);
  }, []);

  if (!state) return null;
  const page = view ? state.pages.find((p) => p.id === view) : undefined;

  return (
    <div ref={panel}>
      <Panel title="Setup wizard" collapseId="wizard">
        {page ? (
          <PageView
            {...props}
            key={page.id}
            page={page}
            state={state}
            operations={operations}
            description={description}
            onState={setState}
            onGo={setView}
          />
        ) : (
          <Overview state={state} onGo={setView} />
        )}
      </Panel>
    </div>
  );
}

function Overview({
  state,
  onGo,
}: {
  state: WizardState;
  onGo: (page: WizardPage["id"]) => void;
}) {
  const parts: [1 | 2, string][] = [
    [1, "Set up the machine"],
    [2, "Calibrate"],
  ];
  return (
    <>
      <p className={`note${state.stage === "calibrated" ? " text-muted" : " note-warn"}`}>
        {stageMessage(state)}
      </p>
      <button
        type="button"
        className="button primary"
        onClick={() => onGo(state.next_page ?? "done")}
      >
        <ArrowRight size={16} aria-hidden />
        {state.next_page ? "Continue" : "Summary"}
      </button>
      {parts.map(([part, heading]) => (
        <div key={part}>
          <h3 className="wizard-part">{heading}</h3>
          <ol className="calibration-list">
            {state.pages
              .filter((p) => p.part === part && p.id !== "welcome")
              .map((p) => (
                <li key={p.id} className={`calibration-line wizard-${p.status}`}>
                  <button type="button" className="wizard-row" onClick={() => onGo(p.id)}>
                    <span className="calibration-number">{p.number}</span>
                    <span className="calibration-name">
                      {p.title}
                      {p.optional && <span className="text-faint"> (optional)</span>}
                    </span>
                    <span className="calibration-chip">
                      {STATUS[p.status].icon}
                      {STATUS[p.status].label}
                    </span>
                  </button>
                </li>
              ))}
          </ol>
        </div>
      ))}
    </>
  );
}

function PageView({
  page,
  state,
  description,
  operations,
  canRun,
  canSet,
  canConfirm,
  onAction,
  onStop,
  onState,
  onGo,
}: Props & {
  page: WizardPage;
  state: WizardState;
  onState: (state: WizardState) => void;
  onGo: (page: WizardPage["id"] | null) => void;
}) {
  const text = PAGE_TEXT[page.id];
  const { run, pending, error } = useAction();
  const [ticked, setTicked] = useState<boolean[]>(() => text.before.map(() => false));
  const ready = ticked.every(Boolean);
  const index = state.pages.findIndex((p) => p.id === page.id);
  const previous = state.pages[index - 1];
  const next = state.pages[index + 1];

  const kind = page.action ? ACTION_KIND[page.action] : null;
  const ops = Object.values(operations).filter((op) => op.kind === kind);
  const running = ops.find((op) => op.status === "running");
  const failed = ops.at(-1)?.status === "failed" ? ops.at(-1) : undefined;

  const setPage = async (status: "done" | "skipped" | "todo") => {
    if (page.id !== "nozzle_tips" && page.id !== "discard_park" && page.id !== "strips_trays") {
      return;
    }
    const updated = await run(() =>
      api.PUT("/api/v1/wizard/pages/{page_id}", {
        params: { path: { page_id: page.id as "nozzle_tips" } },
        body: { status },
      }),
    );
    if (updated) {
      onState(updated);
      if (status !== "todo") onGo(next?.id ?? null);
    }
  };

  return (
    <div className="wizard-page">
      <div className="calibration-title">
        <span className="wizard-count">
          Page {page.number} of {state.pages.length}
        </span>
        <span className="calibration-name wizard-title">{page.title}</span>
        <span className={`calibration-chip wizard-${page.status}`}>
          {STATUS[page.status].icon}
          {STATUS[page.status].label}
        </span>
      </div>

      <section>
        <h3 className="wizard-part">What this learns</h3>
        <p>{text.learns}</p>
      </section>

      {page.id === "welcome" && <Overview state={state} onGo={onGo} />}

      {text.before.length > 0 && (
        <section>
          <h3 className="wizard-part">Before you start</h3>
          <ul className="wizard-before">
            {text.before.map((item, i) => (
              <li key={item}>
                <label>
                  <input
                    type="checkbox"
                    checked={ticked[i]}
                    onChange={(e) =>
                      setTicked(ticked.map((t, j) => (j === i ? e.target.checked : t)))
                    }
                  />
                  {item}
                </label>
              </li>
            ))}
          </ul>
        </section>
      )}

      {text.happens && (
        <section>
          <h3 className="wizard-part">What will happen</h3>
          <p>{text.happens}</p>
        </section>
      )}

      {page.id === "nozzle_tips" && <NozzleTips description={description} canSet={canConfirm} />}
      {page.id === "discard_park" && <MachineLocations description={description} canSet={canSet} />}

      <div className="button-row">
        {page.action && page.action !== "calibrate_feeders" && (
          <button
            type="button"
            className="button primary"
            disabled={!canRun || !ready || !!running}
            onClick={() => onAction(page.action!)}
            title={ready ? undefined : "Tick each item under Before you start first"}
          >
            <Play size={16} aria-hidden />
            {page.status === "done" || page.status === "stale" ? "Redo" : "Start"}
          </button>
        )}
        {page.confirm && page.status !== "done" && (
          <button
            type="button"
            className="button primary"
            disabled={!canConfirm || pending}
            onClick={() => void setPage("done")}
          >
            <Check size={16} aria-hidden />
            {page.id === "strips_trays" ? "Done" : "Confirm"}
          </button>
        )}
        {page.optional && page.status !== "skipped" && page.status !== "done" && (
          <button
            type="button"
            className="button"
            disabled={!canConfirm || pending}
            onClick={() => void setPage("skipped")}
          >
            <SkipForward size={16} aria-hidden />
            Skip
          </button>
        )}
        {page.confirm && (page.status === "done" || page.status === "skipped") && (
          <button
            type="button"
            className="button"
            disabled={!canConfirm || pending}
            onClick={() => void setPage("todo")}
          >
            Open again
          </button>
        )}
      </div>

      {running && (
        <section>
          <h3 className="wizard-part">During</h3>
          <p className="note">Running: watch the camera views. Stop if anything looks wrong.</p>
          <button type="button" className="button" onClick={onStop}>
            <Square size={16} aria-hidden />
            Stop
          </button>
          <p className="text-faint">
            Stop halts the machine at once; reset and home it, then start this page again.
          </p>
        </section>
      )}

      {page.id !== "welcome" && (
        <section>
          <h3 className="wizard-part">Result</h3>
          <p>
            {page.detail}
            {page.measured_at && (
              <span className="text-faint"> ({new Date(page.measured_at).toLocaleString()})</span>
            )}
          </p>
          {page.stale && <p className="text-warn">Out of date: {page.stale}. Redo it.</p>}
          {page.warning && <p className="text-warn">{page.warning}.</p>}
          {failed && <p className="note note-danger">{failed.error}</p>}
          {error && <p className="note note-danger">{error}</p>}
        </section>
      )}

      <div className="button-row wizard-nav">
        <button
          type="button"
          className="button"
          disabled={!previous}
          onClick={() => previous && onGo(previous.id)}
        >
          <ArrowLeft size={16} aria-hidden />
          Back
        </button>
        {next && (
          <button type="button" className="button" onClick={() => onGo(next.id)}>
            <ArrowRight size={16} aria-hidden />
            Next
          </button>
        )}
        <button type="button" className="button" onClick={() => onGo(null)}>
          <LogOut size={16} aria-hidden />
          Exit, and finish later
        </button>
      </div>
    </div>
  );
}
