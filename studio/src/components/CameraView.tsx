import { Camera, CameraOff } from "lucide-react";
import { useEffect, useState } from "react";
import { mediaUrl } from "../api/client";
import { useCore } from "../api/core";
import type { CameraInfo } from "../api/types";
import { Panel } from "./Panel";

/** Stream width: twice the sidebar width, so it stays sharp on high-density screens. */
const STREAM_WIDTH = 640;
const STREAM_FPS = 15;
const RETRY_MS = 2000;

interface Props {
  id: string;
  title: string;
  info?: CameraInfo;
}

/**
 * A live camera feed (MJPEG) with a crosshair overlay. The overlay is drawn here, not in
 * the video, so it stays sharp at any stream quality (spec section 6.5).
 */
export function CameraView({ id, title, info }: Props) {
  return (
    <Panel title={title} collapseId={`camera-${id}`}>
      <CameraFeed id={id} info={info} />
    </Panel>
  );
}

function CameraFeed({ id, info }: { id: string; info?: CameraInfo }) {
  const { status, clientId } = useCore();
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);

  // After a failure, try again shortly with a fresh URL.
  useEffect(() => {
    if (!failed) return;
    const timer = window.setTimeout(() => {
      setFailed(false);
      setAttempt((n) => n + 1);
    }, RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [failed]);

  const aspect = info?.width && info.height ? `${info.width} / ${info.height}` : "16 / 9";
  const off = info?.source === "none";

  if (status !== "open" || off) {
    return (
      <div className="camera-view camera-placeholder" style={{ aspectRatio: aspect }}>
        {off ? (
          <>
            <CameraOff size={28} strokeWidth={1.25} aria-hidden />
            <span>Camera not configured</span>
          </>
        ) : (
          <>
            <Camera size={28} strokeWidth={1.25} aria-hidden />
            <span>Waiting for the core…</span>
          </>
        )}
      </div>
    );
  }

  // The client ID changes on every reconnect, which restarts the stream.
  const src = mediaUrl(`/api/v1/cameras/${id}/stream.mjpg`, {
    width: STREAM_WIDTH,
    fps: STREAM_FPS,
    quality: 70,
    session: `${clientId ?? ""}-${attempt}`,
  });

  return (
    <div className="camera-view camera-live" style={{ aspectRatio: aspect }}>
      {!failed && (
        <img src={src} alt={`${id} camera`} onError={() => setFailed(true)} draggable={false} />
      )}
      {failed && (
        <span className="camera-message">
          <CameraOff size={20} strokeWidth={1.5} aria-hidden />
          {info?.error ?? "No video"}
        </span>
      )}
      <svg className="camera-overlay" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
        <line x1="50" y1="0" x2="50" y2="100" />
        <line x1="0" y1="50" x2="100" y2="50" />
      </svg>
      {info?.source === "synthetic" && <span className="camera-badge">simulated</span>}
    </div>
  );
}
