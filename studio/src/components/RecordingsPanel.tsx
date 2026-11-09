import { Circle, Download, Square, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import type { RecordingInfo } from "../api/types";
import { Panel } from "./Panel";

const POLL_MS = 2000;

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

const size = (bytes: number) =>
  bytes >= 1e9
    ? `${(bytes / 1e9).toFixed(1)} GB`
    : bytes >= 1e6
      ? `${(bytes / 1e6).toFixed(0)} MB`
      : `${Math.max(1, Math.round(bytes / 1e3))} kB`;

/**
 * Flight recordings: every frame vision looks at and everything the machine does, kept in
 * the core's data folder, to download and send to PicnicPlace's developers.
 */
export function RecordingsPanel() {
  const { status, operations } = useCore();
  const { run, pending, error } = useAction();
  const [label, setLabel] = useState("");
  const [list, setList] = useState<RecordingInfo[]>([]);
  const [downloading, setDownloading] = useState<string | null>(null);
  const recording = list.find((r) => r.recording);
  const finished = Object.values(operations).filter((o) => o.status !== "running").length;

  const refresh = async () => {
    const { data } = await api.GET("/api/v1/recordings");
    if (data) setList(data);
  };

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    const fetch = () =>
      void api.GET("/api/v1/recordings").then(({ data }) => {
        if (data && !cancelled) setList(data);
      });
    fetch();
    const timer = recording ? window.setInterval(fetch, POLL_MS) : undefined;
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [status, finished, !!recording]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    if (await run(() => api.POST("/api/v1/recordings", { body: { label } }))) {
      setLabel("");
      await refresh();
    }
  };
  const stop = async () => {
    await run(() => api.POST("/api/v1/recordings/stop"));
    await refresh();
  };
  const remove = async (id: string) => {
    await run(() =>
      api.DELETE("/api/v1/recordings/{recording_id}", {
        params: { path: { recording_id: id } },
      }),
    );
    await refresh();
  };
  const download = async (id: string) => {
    setDownloading(id);
    try {
      const { data } = await api.GET("/api/v1/recordings/{recording_id}/download", {
        params: { path: { recording_id: id } },
        parseAs: "blob",
      });
      if (!data) return;
      const url = URL.createObjectURL(data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${id}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setDownloading(null);
    }
  };

  return (
    <Panel title="Flight recorder" collapseId="recordings">
      <p className="text-muted note">
        Keeps every frame vision looks at and everything the machine does, to send to
        PicnicPlace's developers when something goes wrong. A recording holds photos of your
        boards and, for a run, the job.
      </p>
      {recording ? (
        <div className="bringup-row">
          <span className="rec-live">
            <Circle size={10} aria-hidden /> Recording {recording.label}: {recording.frames}{" "}
            frames, {size(recording.bytes)}
          </span>
          <button type="button" className="button" disabled={pending} onClick={() => void stop()}>
            <Square size={14} aria-hidden />
            Stop
          </button>
        </div>
      ) : (
        <div className="bringup-row">
          <input
            className="input"
            placeholder="What it is for, such as: slot 3 mispicks"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !pending && void start()}
          />
          <button
            type="button"
            className="button"
            disabled={pending || status !== "open"}
            onClick={() => void start()}
          >
            <Circle size={14} aria-hidden />
            Record
          </button>
        </div>
      )}
      <ul className="item-list rec-list" aria-label="Recordings">
        {list
          .filter((r) => !r.recording)
          .map((r) => (
            <li key={r.id} className="rec-row">
              <span className="rec-what">
                <span>{r.label}</span>
                <span className="text-muted">
                  {when(r.started_at)} · {r.frames} frames · {size(r.bytes)}
                  {r.outcome ? ` · ${r.outcome}` : ""}
                </span>
              </span>
              <button
                type="button"
                className="button button-sm"
                disabled={downloading !== null}
                title="Download it as one zip file"
                onClick={() => void download(r.id)}
              >
                <Download size={14} aria-hidden />
                {downloading === r.id ? "…" : "Zip"}
              </button>
              <button
                type="button"
                className="button button-sm button-warn"
                disabled={pending}
                title="Delete it"
                aria-label={`Delete ${r.label}`}
                onClick={() => void remove(r.id)}
              >
                <Trash2 size={14} aria-hidden />
              </button>
            </li>
          ))}
      </ul>
      {error && <p className="note text-danger">{error}</p>}
    </Panel>
  );
}
