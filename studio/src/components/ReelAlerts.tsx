import { CircleAlert, X } from "lucide-react";
import { useState } from "react";
import { useFeeders } from "../api/useFeeders";
import { lowReels } from "../reels";

/**
 * A popup for each feeder whose reel, as counted, is down to the level its owner set to be
 * warned at (and again when it should be empty). Hidden until the reel is low again: a
 * reel set to a fuller count, then run down, warns again.
 */
export function ReelAlerts() {
  const [feeders] = useFeeders();
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const low = feeders ? lowReels(feeders) : [];
  const lowKeys = low.map((r) => r.key).join(" ");

  // Forget what was hidden for reels no longer low (refilled, or their count reset), as
  // the low ones change.
  const [lastKeys, setLastKeys] = useState(lowKeys);
  if (lastKeys !== lowKeys) {
    setLastKeys(lowKeys);
    const now = new Set(lowKeys.split(" "));
    setHidden(new Set([...hidden].filter((k) => now.has(k))));
  }

  const shown = low.filter((r) => !hidden.has(r.key));
  if (!shown.length) return null;
  return (
    <div className="reel-alerts" role="status" aria-live="polite">
      {shown.map((r) => (
        <section key={r.key} className="reel-alert">
          <CircleAlert size={18} aria-hidden className="reel-alert-icon" />
          <p className="reel-alert-text">
            <strong>
              {r.label}: {r.left === 0 ? "reel should be empty" : `reel low, ${r.left} left`}
            </strong>
            <br />
            {r.partId} (warns at {r.low}).{" "}
            {r.left === 0
              ? "Load a new reel and set its count in the Feeders workspace."
              : "Have the next reel ready."}
          </p>
          <button
            type="button"
            className="icon-button"
            aria-label="Hide"
            title="Hide until it is low again"
            onClick={() => setHidden((was) => new Set([...was, r.key]))}
          >
            <X size={16} aria-hidden />
          </button>
        </section>
      ))}
    </div>
  );
}
