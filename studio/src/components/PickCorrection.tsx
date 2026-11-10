import { useState } from "react";
import { api } from "../api/client";
import type { FeedersState, PickCorrection, PickLearning, PickMethod } from "../api/types";

const LABELS: Record<PickCorrection, string> = {
  auto: "Auto: bottom camera, top camera when needed",
  top: "Top camera: find each pocket first",
  bottom: "Bottom camera: learn from earlier picks",
  off: "Neither: pick where the setup says",
};

const SHORT: Record<PickCorrection, string> = {
  auto: "auto",
  top: "top camera",
  bottom: "bottom camera",
  off: "neither",
};

/** How to keep picks centered on their parts. Empty: the config's choice. */
export function PickCorrectionSelect({
  value,
  onChange,
  configured,
}: {
  value: PickCorrection | null;
  onChange: (value: PickCorrection | null) => void;
  /** The config's choice, if known. */
  configured?: PickCorrection;
}) {
  return (
    <select
      className="input"
      value={value ?? ""}
      onChange={(e) => onChange((e.target.value || null) as PickCorrection | null)}
    >
      <option value="">As configured{configured ? ` (${SHORT[configured]})` : ""}</option>
      {(Object.keys(LABELS) as PickCorrection[]).map((k) => (
        <option key={k} value={k}>
          {LABELS[k]}
        </option>
      ))}
    </select>
  );
}

/** What the bottom camera has learned of one feeder's picks, and a way to forget it. */
export function LearnedPicks({
  sourceId,
  learned,
  onChanged,
}: {
  sourceId: string;
  learned: PickLearning | null;
  onChanged: (state: FeedersState) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  if (!learned) {
    return (
      <p className="text-muted note">
        Bottom camera: nothing learned yet. Each part it aligns tells it where this feeder's
        parts really lie.
      </p>
    );
  }
  const fmt = (v: number) => (v >= 0 ? "+" : "") + v.toFixed(2);
  const misses = (["top", "bottom", "off"] as PickMethod[]).flatMap((k) => {
    const m = learned.misses[k];
    if (!m) return [];
    return [`${SHORT[k]} ${m.mean.toFixed(2)} mm (${m.count} pick${m.count === 1 ? "" : "s"})`];
  });
  const forget = async () => {
    const { data } = await api.DELETE("/api/v1/feeders/learned/{source_id}", {
      params: { path: { source_id: sourceId } },
    });
    if (data) onChanged(data);
    setError(data ? null : "Could not forget it.");
  };
  return (
    <div className="learned-picks">
      <p>
        <span className="text-muted">Bottom camera: parts lie </span>
        <span className="mono">
          {fmt(learned.offset.x)}, {fmt(learned.offset.y)} mm
        </span>
        <span className="text-muted">
          {" "}
          from where the setup says, from {learned.samples} pick{learned.samples === 1 ? "" : "s"}.
        </span>
      </p>
      {misses.length > 0 && (
        <p className="text-muted">
          Parts sat off the nozzle's center by, on average: {misses.join(" · ")}.
        </p>
      )}
      <div className="button-row">
        <button type="button" className="button" onClick={() => void forget()}>
          Forget what it learned
        </button>
      </div>
      {error && <p className="note text-danger">{error}</p>}
    </div>
  );
}
