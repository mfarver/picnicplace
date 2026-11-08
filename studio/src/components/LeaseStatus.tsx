import { Hand, LogOut } from "lucide-react";
import { useEffect, useState } from "react";
import { hasControl, useCore } from "../api/core";
import { describeClient } from "../format";

/** How long the "confirm take control" step stays armed. */
const CONFIRM_MS = 4000;

interface Props {
  busy: boolean;
  onTake: () => void;
  onRelease: () => void;
}

/**
 * Who controls the machine (spec section 5.2). Taking control from another client is a
 * two-step action, so a stray click cannot steal it.
 */
export function LeaseStatus({ busy, onTake, onRelease }: Props) {
  const core = useCore();
  const holder = core.lease?.holder;
  const mine = hasControl(core);
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);

  if (mine) {
    return (
      <div className="lease lease-mine">
        <span>You have control.</span>
        <button type="button" className="button button-sm" disabled={busy} onClick={onRelease}>
          <LogOut size={14} aria-hidden />
          Release
        </button>
      </div>
    );
  }
  if (!holder) {
    return (
      <p className="lease note text-muted">No one has control. Moving the machine takes it.</p>
    );
  }
  return (
    <div className="lease lease-other">
      <span>
        <strong>{describeClient(holder)}</strong> has control.
      </span>
      <button
        type="button"
        className={`button button-sm${armed ? " button-warn" : ""}`}
        disabled={busy}
        onClick={() => {
          if (armed) {
            setArmed(false);
            onTake();
          } else {
            setArmed(true);
          }
        }}
      >
        <Hand size={14} aria-hidden />
        {armed ? "Confirm take control" : "Take control"}
      </button>
    </div>
  );
}
