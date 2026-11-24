import { Check, ClipboardCheck, Eye, Play, RefreshCw } from "lucide-react";
import { mediaUrl } from "../api/client";
import type { RunItem, RunState } from "../api/types";

/**
 * The review at the end of a run: every part is placed, and the placements the top camera
 * found off, turned, or not there are listed together, each with its picture. Fine leaves
 * one as it is; Place again (once its part is taken off) has the machine place it again.
 * Finish ends the run, placing those again first.
 */
export function RunReview({
  run,
  canSignal,
  canMove,
  canLook,
  onReview,
  onLook,
  onFinish,
}: {
  run: RunState;
  canSignal: boolean;
  canMove: boolean;
  canLook: boolean;
  onReview: (item: RunItem, placeAgain: boolean) => void;
  onLook: (item: RunItem) => void;
  onFinish: () => void;
}) {
  const items = run.to_review.map((i) => run.plan.items[i]).filter((i) => i !== undefined);
  const again = run.plan.items.filter((i) => i.status === "pending").length;
  return (
    <div className="run-alert run-review" role="region" aria-label="Placements to look at">
      <p className="run-alert-title">
        <ClipboardCheck size={18} aria-hidden />
        {items.length
          ? `Look at ${items.length} placement${items.length === 1 ? "" : "s"}`
          : "Reviewed"}
      </p>
      <p className="run-alert-text">
        {items.length
          ? "Every part is placed. The top camera found these off their pads, turned, or not " +
            "there. Leave each as it is, or take its part off and place it again."
          : again
            ? "Every placement is looked at. Finish places the ones taken off again, then " +
              "ends the run."
            : "Every placement is looked at. Finish ends the run."}
      </p>
      <ul className="run-review-list">
        {items.map((item) => (
          <li key={item.index} className="run-review-item">
            {item.check?.picture && (
              <img
                className="run-review-picture"
                src={mediaUrl(`/api/v1/run/checks/${item.index}`, {
                  at: run.started_at,
                  score: item.check.score,
                })}
                alt={`${item.ref} as the top camera saw it`}
              />
            )}
            <div className="run-review-text">
              <strong>{item.copy_id ? `${item.ref} ·${item.copy_id}` : item.ref}</strong>
              <span className="text-muted">{item.note}</span>
              <div className="button-row">
                <button
                  type="button"
                  className="button"
                  disabled={!canSignal}
                  title="Leave it as it is"
                  onClick={() => onReview(item, false)}
                >
                  <Check size={16} aria-hidden />
                  Fine
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={!canSignal}
                  title="You have taken its part off the board: place it again"
                  onClick={() => onReview(item, true)}
                >
                  <RefreshCw size={16} aria-hidden />
                  Place again
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={!canLook}
                  title="Move the top camera over it, its footprint drawn on the picture"
                  onClick={() => onLook(item)}
                >
                  <Eye size={16} aria-hidden />
                  Look
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>
      <div className="button-row">
        <button type="button" className="button primary" disabled={!canMove} onClick={onFinish}>
          <Play size={16} aria-hidden />
          {again ? `Place ${again} again and finish` : "Finish"}
        </button>
      </div>
    </div>
  );
}
