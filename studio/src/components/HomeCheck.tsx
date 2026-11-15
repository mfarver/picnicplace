import { House } from "lucide-react";
import { useEffect, useRef } from "react";
import type { HomeQuestion } from "../api/useHomeCheck";

/** The question `useHomeCheck` asks before homing: are the nozzles level? */
export function HomeCheck({ question }: { question: HomeQuestion | null }) {
  return question && <HomeCheckDialog {...question} />;
}

function HomeCheckDialog({ sensorUnsafe, refused, onCancel, onHome }: HomeQuestion) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return (
    <dialog ref={ref} className="home-check" onCancel={onCancel} aria-labelledby="home-check">
      <h2 id="home-check">Are the nozzles level?</h2>
      {refused ? (
        <p className="note note-warn">
          The safe-Z sensor reads not safe: a nozzle is low. Raise the nozzles by hand until it
          reads safe; the machine will not home until then.
        </p>
      ) : (
        sensorUnsafe && <p className="note note-warn">The safe-Z sensor reads not safe.</p>
      )}
      <p>
        Homing moves the head across the machine to its switches. A nozzle hanging lower than
        the other can be dragged into feeders, boards or the bed on the way.
      </p>
      <p>
        Look at the nozzles from the front: both tips should be at about the same height. If
        one hangs low, bring them level by hand first.
      </p>
      <p className="text-muted">
        Keep a hand on the machine's power. Unplugging the controller, or pressing its reset
        button, stops all motion at once.
      </p>
      {/* Cancel comes first, so it has the focus: Enter alone never homes. */}
      <div className="button-row">
        <button type="button" className="button" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="button primary" disabled={refused} onClick={onHome}>
          <House size={16} aria-hidden />
          They're level: home
        </button>
      </div>
    </dialog>
  );
}
