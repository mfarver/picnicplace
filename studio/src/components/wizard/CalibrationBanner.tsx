import { ArrowRight, TriangleAlert } from "lucide-react";
import { hasControl, useCore } from "../../api/core";
import { openWizard, useWizard } from "../../api/useWizard";
import { stageMessage } from "./wizardPages";

/**
 * Across the top of every workspace until the machine is calibrated: what is limited,
 * the next page to do, and Continue calibration, which opens the setup wizard there. It
 * cannot be dismissed. A Studio another client controls sees it without the button.
 */
export function CalibrationBanner({ onOpen }: { onOpen: () => void }) {
  const core = useCore();
  const [state] = useWizard();
  if (!state || state.stage === "calibrated") return null;
  const canContinue = !core.lease?.holder || hasControl(core);
  const next = state.pages.find((p) => p.id === state.next_page);

  return (
    <div className="version-banner calibration-banner" role="status">
      <TriangleAlert size={16} aria-hidden />
      <span>
        {stageMessage(state)}
        {next && ` Next: ${next.title}.`}
        {!state.enforced && " (The simulator is not held to these limits.)"}
      </span>
      {canContinue && (
        <button
          type="button"
          className="button"
          onClick={() => {
            onOpen();
            // After the Machine workspace is shown, so its wizard hears the request.
            setTimeout(() => openWizard(state.next_page), 0);
          }}
        >
          <ArrowRight size={14} aria-hidden />
          Continue calibration
        </button>
      )}
    </div>
  );
}
