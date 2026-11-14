import { api } from "../api/client";
import { useAction } from "../api/useAction";
import { publishDescription } from "../api/useDescription";
import type { MachineProfile } from "../api/types";

/**
 * Which tip is fitted on each nozzle, said by the operator after changing one by hand.
 * The core keeps it, and uses the nozzle's Z offset and vacuum levels measured with that
 * tip; a tip not measured yet marks the nozzle offsets out of date.
 */
export function NozzleTips({
  description,
  canSet,
}: {
  description: MachineProfile;
  /** This client may change the machine. */
  canSet: boolean;
}) {
  const { run, pending, error } = useAction();
  if (description.nozzle_tips.length === 0) return null;

  const fit = async (nozzleId: string, tipId: string) => {
    const profile = await run(() =>
      api.PUT("/api/v1/machine/nozzles/{nozzle_id}/tip", {
        params: { path: { nozzle_id: nozzleId } },
        body: { tip_id: tipId === "" ? null : tipId },
      }),
    );
    if (profile) publishDescription(profile);
  };

  return (
    <div className="nozzle-tips">
      <p className="text-muted note">
        After changing a tip by hand, say which one is on. Calibrate the nozzle offsets once
        with each tip; its heights and vacuum levels come back when it is fitted again.
      </p>
      {description.nozzles.map((nozzle) => (
        <label key={nozzle.id} className="nozzle-tip">
          <span className="nozzle-tip-name">{nozzle.label}</span>
          <select
            className="input"
            value={nozzle.tip ?? ""}
            disabled={!canSet || pending}
            onChange={(e) => void fit(nozzle.id, e.target.value)}
          >
            <option value="">Not said</option>
            {description.nozzle_tips.map((tip) => (
              <option key={tip.id} value={tip.id}>
                {tip.label}
              </option>
            ))}
          </select>
        </label>
      ))}
      {error && <p className="note note-danger">{error}</p>}
    </div>
  );
}
