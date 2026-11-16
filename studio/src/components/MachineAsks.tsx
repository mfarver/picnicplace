import { CircleStop, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { hasControl, useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { publishDescription, useDescription } from "../api/useDescription";
import type { MachineProfile, OperatorRequest } from "../api/types";

/**
 * When the machine needs a person, it asks (spec §8.2): a dialog over every workspace
 * while the machine state's `request` is set, saying what to do and what happens after.
 * The client in control answers it, or stops the operation. Today it asks for tip changes.
 */
export function MachineAsks() {
  const core = useCore();
  const description = useDescription();
  const request = core.machine?.request ?? null;
  const asked = useRef(false);

  // Answering changes the machine's tips: show them once the machine stops asking.
  useEffect(() => {
    if (request) asked.current = true;
    else if (asked.current) {
      asked.current = false;
      void api.GET("/api/v1/machine/description").then(({ data }) => {
        if (data) publishDescription(data);
      });
    }
  }, [request]);

  if (!request || !description) return null;
  return (
    <AskDialog
      key={request.id}
      request={request}
      description={description}
      canAnswer={hasControl(core)}
    />
  );
}

function AskDialog({
  request,
  description,
  canAnswer,
}: {
  request: OperatorRequest;
  description: MachineProfile;
  canAnswer: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const { run, pending, error } = useAction();
  // Each nozzle starts at the tip on it, if that would do, else the first that would.
  const [tips, setTips] = useState<Record<string, string | null>>(() =>
    Object.fromEntries(
      request.nozzles.map((n) => [
        n.nozzle_id,
        n.choices.length === 0 || (n.fitted && n.choices.includes(n.fitted))
          ? n.fitted
          : n.choices[0]!,
      ]),
    ),
  );
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  const label = (id: string) => description.nozzles.find((n) => n.id === id)?.label ?? id;
  const answer = () =>
    void run(() =>
      api.POST("/api/v1/machine/answer", { body: { request_id: request.id, tips } }),
    );
  const stop = () => void run(() => api.POST("/api/v1/machine/stop"));

  return (
    <dialog
      ref={ref}
      className="machine-asks"
      aria-labelledby="machine-asks"
      // Escape does not dismiss it: the machine is still waiting. Stop ends the operation.
      onCancel={(e) => e.preventDefault()}
    >
      <h2 id="machine-asks">{request.title}</h2>
      <p>{request.message}</p>
      {request.nozzles.map((n) => {
        const fits = new Set(n.choices);
        const others = description.nozzle_tips.filter((t) => !fits.has(t.id));
        return (
          <label key={n.nozzle_id} className="nozzle-tip">
            <span className="nozzle-tip-name">{label(n.nozzle_id)}</span>
            <select
              className="input"
              value={tips[n.nozzle_id] ?? ""}
              disabled={!canAnswer || pending}
              onChange={(e) =>
                setTips({ ...tips, [n.nozzle_id]: e.target.value === "" ? null : e.target.value })
              }
            >
              {n.choices.length > 0 ? (
                <>
                  <optgroup label="Would do">
                    {n.choices.map((id) => (
                      <option key={id} value={id}>
                        {description.nozzle_tips.find((t) => t.id === id)?.label ?? id}
                      </option>
                    ))}
                  </optgroup>
                  <optgroup label="Others">
                    <option value="">None</option>
                    {others.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.label}
                      </option>
                    ))}
                  </optgroup>
                </>
              ) : (
                <>
                  <option value="">None</option>
                  {others.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.label}
                    </option>
                  ))}
                </>
              )}
            </select>
          </label>
        );
      })}
      <p className="text-muted">{request.after}</p>
      {!canAnswer && (
        <p className="note note-warn">Another client is in control: it answers, or takes Stop.</p>
      )}
      {error && <p className="note note-danger">{error}</p>}
      <div className="button-row">
        <button type="button" className="button" disabled={pending} onClick={stop}>
          <CircleStop size={16} aria-hidden />
          Stop
        </button>
        <button
          type="button"
          className="button primary"
          disabled={!canAnswer || pending}
          onClick={answer}
        >
          <Play size={16} aria-hidden />
          Done: carry on
        </button>
      </div>
    </dialog>
  );
}
