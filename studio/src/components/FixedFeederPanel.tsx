import { Plus, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { useAction } from "../api/useAction";
import type { FeedersState, FixedFeederState } from "../api/types";
import { remainingText } from "./machine/fixedFeederMath";
import { Panel } from "./Panel";

/** Strips and trays on the bed: parts left in each, and a refill button. */
export function FixedFeederPanel({
  fixed,
  onChanged,
  onSelect,
  selected,
  onAdd,
}: {
  fixed: FixedFeederState[];
  onChanged: (state: FeedersState) => void;
  onSelect?: (id: string) => void;
  selected?: string | null;
  /** Offer to add one. */
  onAdd?: () => void;
}) {
  const { run, pending, error } = useAction();

  const refill = async (id: string) => {
    const done = await run(() =>
      api.POST("/api/v1/feeders/fixed/{fixed_id}/refill", {
        params: { path: { fixed_id: id } },
        body: { picked: 0 },
      }),
    );
    if (!done) return;
    const { data } = await api.GET("/api/v1/feeders");
    if (data) onChanged(data);
  };

  return (
    <Panel title="Strips and trays" collapseId="fixed-feeders">
      <ul className="item-list ff-list" aria-label="Strips and trays">
        {fixed.map((s) => {
          const f = s.feeder;
          const capacity = f.columns * f.rows;
          const level = capacity ? s.remaining / capacity : 0;
          const body = (
            <>
              <span className="item-id">
                {s.id}
                {f.label && <span className="text-muted"> {f.label}</span>}
              </span>
              <span className="item-detail">
                {f.part_id ?? "no part"}
                {" · "}
                <span className={s.remaining === 0 ? "text-danger" : undefined}>
                  {remainingText(s)}
                </span>
                {!f.enabled && <span className="chip">off</span>}
              </span>
              <span className="ff-meter" aria-hidden>
                <span
                  className={s.remaining === 0 ? "ff-meter-empty" : level <= 0.2 ? "ff-meter-low" : ""}
                  style={{ width: `${Math.round(level * 100)}%` }}
                />
              </span>
            </>
          );
          return (
            <li key={s.id} className="ff-row">
              {onSelect ? (
                <button
                  type="button"
                  className={s.id === selected ? "item active" : "item"}
                  onClick={() => onSelect(s.id)}
                >
                  {body}
                </button>
              ) : (
                <div className="item">{body}</div>
              )}
              <button
                type="button"
                className="button button-sm"
                disabled={pending || f.picked === 0}
                title="It has been refilled: start again from its first part"
                onClick={() => void refill(s.id)}
              >
                <RefreshCw size={14} aria-hidden />
                Refilled
              </button>
            </li>
          );
        })}
        {fixed.length === 0 && (
          <li className="text-muted item-empty">
            None yet. Add one for parts held on the bed: a cut strip of tape in a holder, or a
            tray.
          </li>
        )}
      </ul>
      {onAdd && (
        <div className="button-row">
          <button type="button" className="button" onClick={onAdd}>
            <Plus size={16} aria-hidden />
            Add strip or tray
          </button>
        </div>
      )}
      {error && <p className="note text-danger">{error}</p>}
    </Panel>
  );
}
