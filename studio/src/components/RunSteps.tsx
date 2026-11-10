import {
  Check,
  Circle,
  CircleAlert,
  LoaderCircle,
  Minus,
  RefreshCw,
  SkipForward,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { RunItem, RunState } from "../api/types";
import { focusIndex, runSteps, type RunStepRow } from "../runSteps";

/**
 * A run's steps as a checklist, from the moment it is planned: each one checked off as
 * it is done, and the one under way highlighted (or, when a problem paused the run, the
 * one it stopped at). With ``around``, only that many steps either side of the current
 * one are shown.
 */
export function RunSteps({
  items,
  run,
  fiducials,
  hasBus,
  around,
  large = false,
  canSignal = false,
  onSkip,
}: {
  items: RunItem[];
  /** The run of this job, if one has been started. */
  run: RunState | null;
  fiducials: number;
  hasBus: boolean;
  around?: number;
  /** Large type, for reading from across the room. */
  large?: boolean;
  canSignal?: boolean;
  /** Offer to skip placements (and put skipped ones back). */
  onSkip?: (item: RunItem, skip: boolean) => void;
}) {
  const all = runSteps(items, run, fiducials, hasBus);
  const focus = focusIndex(all);
  const rows = around === undefined ? all : windowAround(all, focus, around);
  const list = useRef<HTMLOListElement>(null);
  const focusKey = all[focus]?.key;

  useFollow(list, focusKey, around === undefined);
  const fit = useFit(list, rows.length, large);

  return (
    <ol className={`run-steps${large ? " run-steps-large" : ""}`} ref={list} style={fit}>
      {rows.map((r) => (
        <li
          key={r.key}
          className={
            "run-step" +
            (r.current ? " run-step-current" : "") +
            (r.problem ? " run-step-problem" : "") +
            (r.held ? " run-step-held" : "") +
            (r.mark !== "todo" ? ` run-step-${r.mark}` : "")
          }
          data-focus={r.key === focusKey || undefined}
          aria-current={r.current ? "step" : undefined}
          title={r.detail ? `${r.label}: ${r.detail}` : undefined}
        >
          <StepIcon row={r} running={run?.status === "running"} size={large ? 20 : 14} />
          <span className="run-step-label">{r.label}</span>
          <span className="run-step-detail">{r.detail}</span>
          {run && r.item && onSkip && (
            <SkipButton item={r.item} run={run} canSignal={canSignal} onSkip={onSkip} />
          )}
        </li>
      ))}
    </ol>
  );
}

/** How long after the operator last scrolled the list it starts following the run again. */
const FOLLOW_AFTER_MS = 8000;

/** The nearest ancestor that scrolls, or the page. */
function scrollParent(el: HTMLElement): HTMLElement {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const y = getComputedStyle(p).overflowY;
    if (y === "auto" || y === "scroll") return p;
  }
  return document.scrollingElement as HTMLElement;
}

/**
 * Keep the current step in view as the run goes, unless the operator is scrolling the
 * list themselves (looking for something): then wait until they have left it alone for
 * a few seconds.
 */
function useFollow(
  list: React.RefObject<HTMLOListElement | null>,
  focusKey: string | undefined,
  enabled: boolean,
) {
  const touched = useRef(0);
  const [idle, setIdle] = useState(0);

  useEffect(() => {
    const el = list.current;
    if (!enabled || !el) return;
    const box = scrollParent(el);
    let timer: number | undefined;
    const onUser = () => {
      touched.current = Date.now();
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setIdle((n) => n + 1), FOLLOW_AFTER_MS);
    };
    const onKey = (e: KeyboardEvent) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) {
        onUser();
      }
    };
    const events = ["wheel", "touchmove", "pointerdown"] as const;
    for (const name of events) box.addEventListener(name, onUser, { passive: true });
    box.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      for (const name of events) box.removeEventListener(name, onUser);
      box.removeEventListener("keydown", onKey);
    };
  }, [list, enabled]);

  useEffect(() => {
    if (!enabled || Date.now() - touched.current < FOLLOW_AFTER_MS) return;
    const el = list.current?.querySelector<HTMLElement>("[data-focus]");
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [list, focusKey, enabled, idle]);
}

/** Row heights for large type: full size when the steps fit, smaller (to a point) when not. */
const ROW_MAX = 44;
const ROW_MIN = 30;

/**
 * For large type, shrink the rows as the job grows so more of it fits on the screen,
 * down to a size still easy to read; beyond that, the list scrolls.
 */
function useFit(
  list: React.RefObject<HTMLOListElement | null>,
  count: number,
  enabled: boolean,
): CSSProperties | undefined {
  const [row, setRow] = useState(ROW_MAX);
  useEffect(() => {
    const el = list.current;
    if (!enabled || !el) return;
    const box = scrollParent(el);
    const measure = () => {
      const top = el.getBoundingClientRect().top - box.getBoundingClientRect().top;
      const room = box.clientHeight - (top + box.scrollTop) - 24;
      setRow(Math.max(ROW_MIN, Math.min(ROW_MAX, Math.floor(room / Math.max(count, 1)))));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [list, count, enabled]);
  if (!enabled) return undefined;
  const t = (row - ROW_MIN) / (ROW_MAX - ROW_MIN);
  return {
    "--run-step-h": `${row}px`,
    "--run-step-font": `${(14 + 4 * t).toFixed(1)}px`,
    "--run-step-detail-font": `${(12 + 3 * t).toFixed(1)}px`,
    "--run-step-icon": `${Math.round(15 + 5 * t)}px`,
  } as CSSProperties;
}

/** ``around`` rows either side of ``focus``, as many as fit, kept full near the ends. */
function windowAround<T>(rows: T[], focus: number, around: number): T[] {
  const size = 2 * around + 1;
  const start = Math.max(0, Math.min(focus - around, rows.length - size));
  return rows.slice(start, start + size);
}

function StepIcon({ row, running, size }: { row: RunStepRow; running: boolean; size: number }) {
  if (row.problem) {
    return <CircleAlert size={size} className="run-step-icon" aria-label="stopped here" />;
  }
  if (row.current && running) {
    return <LoaderCircle size={size} className="run-step-icon spin" aria-label="under way" />;
  }
  switch (row.mark) {
    case "done":
      return <Check size={size} className="run-step-icon" aria-label="done" />;
    case "failed":
      return <X size={size} className="run-step-icon" aria-label="not done" />;
    case "skipped":
      return <Minus size={size} className="run-step-icon" aria-label="left out" />;
    default:
      return <Circle size={size} className="run-step-icon" aria-label="to do" />;
  }
}

function SkipButton({
  item,
  run,
  canSignal,
  onSkip,
}: {
  item: RunItem;
  run: RunState;
  canSignal: boolean;
  onSkip: (item: RunItem, skip: boolean) => void;
}) {
  const operator = item.status === "skipped" && item.note === "skipped by the operator";
  if (item.status !== "pending" && item.status !== "failed" && !operator) return null;
  // A part on a nozzle is placed (or discarded, or taken off by hand) first.
  if (Object.values(run.held).includes(item.index)) return null;
  const label = operator ? `Place ${item.ref} after all` : `Skip ${item.ref}`;
  return (
    <button
      type="button"
      className="icon-button"
      disabled={!canSignal}
      title={label}
      onClick={() => onSkip(item, !operator)}
    >
      {operator ? (
        <RefreshCw size={14} aria-label={label} />
      ) : (
        <SkipForward size={14} aria-label={label} />
      )}
    </button>
  );
}

const STATUS_LABEL: Record<RunItem["status"], string> = {
  pending: "to do",
  placed: "placed",
  skipped: "skipped",
  failed: "failed",
  blocked: "blocked",
};

/** Each placement on the machine map, colored by how far the run has got with it. */
export function RunOverlay({ items }: { items: RunItem[] }) {
  return (
    <g className="run-overlay">
      {items.map((i) => (
        <circle key={i.index} className={`run-mark run-${i.status}`} cx={i.x} cy={-i.y} r={1.1}>
          <title>
            {`${i.ref} ${i.part_id ?? ""}: ${STATUS_LABEL[i.status]}` +
              (i.note ? ` (${i.note})` : "")}
          </title>
        </circle>
      ))}
    </g>
  );
}
