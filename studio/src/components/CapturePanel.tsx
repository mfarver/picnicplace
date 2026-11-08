import { Camera } from "lucide-react";
import { useEffect, useState } from "react";
import { api, mediaUrl } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import type { CaptureInfo } from "../api/types";
import { Panel } from "./Panel";

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** Save full-resolution frames from every camera, for tuning vision on real images. */
export function CapturePanel() {
  const { status } = useCore();
  const { run, pending, error } = useAction();
  const [label, setLabel] = useState("");
  const [recent, setRecent] = useState<CaptureInfo[]>([]);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/cameras/captures").then(({ data }) => {
      if (data && !cancelled) setRecent(data.slice(0, 6));
    });
    return () => {
      cancelled = true;
    };
  }, [status]);

  const capture = async () => {
    const saved = await run(() =>
      api.POST("/api/v1/cameras/captures", { body: { cameras: [], label } }),
    );
    if (saved) setRecent((list) => [...saved, ...list].slice(0, 6));
  };

  return (
    <Panel title="Capture" collapseId="capture">
      <p className="text-muted note">
        Saves a full-resolution, lossless frame from each camera, with the head position and
        lights, to the core's data folder (<code>captures/</code>), for tuning vision.
      </p>
      <div className="bringup-row">
        <input
          className="input"
          placeholder="What it shows, such as: slot 3 pocket"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !pending && void capture()}
        />
        <button
          type="button"
          className="button"
          disabled={pending || status !== "open"}
          onClick={() => void capture()}
        >
          <Camera size={16} aria-hidden />
          Save
        </button>
      </div>
      {error && <p className="note note-danger">{error}</p>}
      <ul className="capture-list">
        {recent.map((c) => (
          <li key={c.id}>
            <a href={mediaUrl(`/api/v1/cameras/captures/${c.id}.png`)} target="_blank" rel="noreferrer">
              {c.camera}
            </a>{" "}
            <span className="text-muted">
              {time(c.taken)}
              {c.label ? `, ${c.label}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
