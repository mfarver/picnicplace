import { Check, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, mediaUrl } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import type { LightColor, TrainedPart, TrainingCandidate } from "../api/types";
import { Panel } from "./Panel";

const NAMES: Record<string, string> = {
  "255,255,255": "White",
  "255,0,0": "Red",
  "0,255,0": "Green",
  "0,0,255": "Blue",
};

function light(color: LightColor | null | undefined): string {
  if (!color) return "Usual light";
  const name =
    NAMES[`${color.red},${color.green},${color.blue}`] ??
    `Color ${color.red}, ${color.green}, ${color.blue}`;
  const level = color.brightness ?? 255;
  return level >= 255 ? `${name}, full` : `${name}, ${Math.round(level / 2.55)}%`;
}

function threshold(value: number | null | undefined): string {
  return value == null ? "usual" : `${Math.round(value * 100)}%`;
}

function method(c: TrainingCandidate): string {
  return c.vision.method === "pads" ? "Pads" : c.vision.method === "outline" ? "Outline" : "Auto";
}

function image(part: TrainedPart, name: string): string {
  return mediaUrl(
    `/api/v1/placement/trained/${encodeURIComponent(part.part_id)}/images/${name}`,
    { at: part.trained_at },
  );
}

/**
 * Parts trained on this machine: how the bottom camera sees each best, with the part's
 * footprint drawn over it and beside it for the operator to approve. Alignment uses a
 * trained part's settings only once approved.
 */
export function TrainedPartsPanel({ canControl }: { canControl: boolean }) {
  const { status, operations } = useCore();
  const { run, pending, error } = useAction();
  const [parts, setParts] = useState<TrainedPart[] | null>(null);
  const [changed, setChanged] = useState(0);
  const finished = Object.values(operations).reduce(
    (latest, o) => (o.finished_at && o.finished_at > latest ? o.finished_at : latest),
    "",
  );

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/placement/trained").then((r) => {
      if (!cancelled && r.data) setParts(r.data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, finished, changed]);

  if (!parts || parts.length === 0) return null;
  const approve = async (id: string) => {
    await run(() =>
      api.POST("/api/v1/placement/trained/{part_id}/approve", {
        params: { path: { part_id: id } },
      }),
    );
    setChanged((n) => n + 1);
  };
  const forget = async (id: string) => {
    await run(() =>
      api.DELETE("/api/v1/placement/trained/{part_id}", { params: { path: { part_id: id } } }),
    );
    setChanged((n) => n + 1);
  };

  return (
    <Panel title="Trained parts" collapseId="trained-parts">
      {parts.map((p) => (
        <section key={p.part_id} className="trained-part">
          <h3 className="trained-title">
            {p.part_id}{" "}
            <span className={p.approved ? "badge-ok" : "badge-warn"}>
              {p.approved ? "Approved" : "Waiting for approval"}
            </span>
          </h3>
          {!p.approved && (
            <p className="note">
              Check that the footprint lines up with the part, pin 1 (red) where the part's
              marking says, then approve. Until then it is seen the usual way.
            </p>
          )}
          <div className="trained-images">
            <figure>
              <img src={image(p, "overlay")} alt={`${p.part_id} with its footprint over it`} />
              <figcaption>Footprint over the part</figcaption>
            </figure>
            <figure>
              <img src={image(p, "beside")} alt={`${p.part_id} with its footprint beside it`} />
              <figcaption>Footprint beside the part, for its markings</figcaption>
            </figure>
          </div>
          <p className="note text-muted">{p.message}</p>
          <details>
            <summary>
              Best of the {p.tried} ways tried (trained with {p.nozzle}
              {p.tip ? `, ${p.tip} tip` : ""})
            </summary>
            <table className="tape-table trained-table">
              <thead>
                <tr>
                  <th>Light</th>
                  <th>By</th>
                  <th>Metal over</th>
                  <th>Found</th>
                  <th>Angle ±°</th>
                  <th>Place ±mm</th>
                  <th>Score</th>
                </tr>
              </thead>
              <tbody>
                {p.candidates.map((c, i) => (
                  <tr key={i}>
                    <td>{light(c.vision.light)}</td>
                    <td>{method(c)}</td>
                    <td>{threshold(c.vision.threshold)}</td>
                    <td>
                      {c.seen} of {c.frames}
                    </td>
                    <td>{c.angle_spread?.toFixed(2) ?? "–"}</td>
                    <td>{c.offset_spread?.toFixed(3) ?? "–"}</td>
                    <td>{c.score?.toFixed(2) ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          <div className="button-row">
            {!p.approved && (
              <button
                type="button"
                className="button primary"
                disabled={!canControl || pending}
                onClick={() => void approve(p.part_id)}
              >
                <Check size={16} aria-hidden />
                Approve
              </button>
            )}
            <button
              type="button"
              className="button"
              disabled={!canControl || pending}
              title="Forget what training found: the part is seen the usual way again"
              onClick={() => void forget(p.part_id)}
            >
              <Trash2 size={16} aria-hidden />
              Forget
            </button>
          </div>
        </section>
      ))}
      {error && <p className="note text-danger">{error}</p>}
    </Panel>
  );
}
