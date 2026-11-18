import { MapPin, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { publishDescription } from "../api/useDescription";
import type { LocalProfileState, MachineProfile } from "../api/types";

export type LocationKind = "discard" | "park" | "paper_pad";

const PLACES: { kind: LocationKind; name: string; what: string }[] = [
  {
    kind: "discard",
    name: "Discard",
    what: "Parts the bottom camera cannot find are dropped here.",
  },
  {
    kind: "park",
    name: "Park",
    what: "The head waits here, out of the way, while a run waits for you.",
  },
  {
    kind: "paper_pad",
    name: "Paper pad",
    what:
      "A small square of plain paper glued flat on the datum board: each tip touches it " +
      "after a tip change, to check it sits as when its height was measured.",
  },
];

/**
 * This machine's own locations (discard and park, unless `kinds` says), set by centering
 * the top camera on them. They are kept in the core's local profile and take effect at
 * once.
 */
export function MachineLocations({
  description,
  canSet,
  kinds = ["discard", "park"],
}: {
  description: MachineProfile;
  /** Homed, and this client may change the machine. */
  canSet: boolean;
  /** The locations shown. */
  kinds?: LocationKind[];
}) {
  const { status } = useCore();
  const { run, pending, error } = useAction();
  const [local, setLocal] = useState<LocalProfileState | null>(null);
  const [changed, setChanged] = useState(0);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/machine/local-profile").then(({ data }) => {
      if (!cancelled && data) setLocal(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, changed]);

  const mine = (kind: LocationKind) =>
    ((local?.profile.locations ?? []) as { kind?: string }[]).some((loc) => loc.kind === kind);

  const act = async (kind: LocationKind, reset: boolean) => {
    const params = { params: { path: { kind } } };
    const profile = await run(() =>
      reset
        ? api.DELETE("/api/v1/machine/locations/{kind}", params)
        : api.PUT("/api/v1/machine/locations/{kind}", { ...params, body: {} }),
    );
    if (profile) publishDescription(profile);
    setChanged((n) => n + 1);
  };

  return (
    <div className="machine-locations">
      <p className="text-muted note">
        Center the top camera over the place, then set it. It is kept for this machine, over
        the machine profile.
      </p>
      {PLACES.filter((p) => kinds.includes(p.kind)).map(({ kind, name, what }) => {
        const at = description.locations.find((loc) => loc.kind === kind);
        const source = !at ? "" : mine(kind) ? "on this machine" : "from the profile or config";
        return (
          <div key={kind} className="machine-location" title={what}>
            <div className="machine-location-text">
              <span className="machine-location-name">{name}</span>
              <span className="mono">
                {at ? `${at.position.x.toFixed(2)}, ${at.position.y.toFixed(2)}` : "not set"}
              </span>
              {source && <span className="text-muted">{source}</span>}
            </div>
            <div className="button-row">
              <button
                type="button"
                className="button"
                disabled={!canSet || pending}
                title={`Make where the top camera is looking the ${name.toLowerCase()} location`}
                onClick={() => void act(kind, false)}
              >
                <MapPin size={16} aria-hidden />
                Set {name.toLowerCase()} here
              </button>
              {mine(kind) && (
                <button
                  type="button"
                  className="button button-icon"
                  disabled={!canSet || pending}
                  title="Forget it: the profile's or config's applies again, if any"
                  aria-label={`Reset the ${name.toLowerCase()} location`}
                  onClick={() => void act(kind, true)}
                >
                  <RotateCcw size={16} aria-hidden />
                </button>
              )}
            </div>
          </div>
        );
      })}
      {error && <p className="note note-danger">{error}</p>}
    </div>
  );
}
