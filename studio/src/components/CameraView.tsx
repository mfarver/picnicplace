import { Camera, CameraOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { mediaUrl } from "../api/client";
import { useCore } from "../api/core";
import { readMjpeg } from "../mjpeg";
import { scaleLength } from "../scaleBar";
import type { CameraInfo } from "../api/types";
import { Panel } from "./Panel";

/** Stream width: twice the sidebar width, so it stays sharp on high-density screens. */
const STREAM_WIDTH = 640;
const STREAM_FPS = 15;
const RETRY_MS = 2000;
const ZOOM_STEP = 1.25;
const MAX_ZOOM = 8;

interface Props {
  id: string;
  title: string;
  info?: CameraInfo;
  /** Width of the view in mm (the profile's, or as measured), for the scale bar. */
  fieldOfView?: number;
}

/**
 * A live camera feed (MJPEG) with a crosshair overlay. The overlay is drawn here, not in
 * the video, so it stays sharp at any stream quality (spec section 6.5).
 */
export function CameraView({ id, title, info, fieldOfView }: Props) {
  return (
    <Panel title={title} collapseId={`camera-${id}`}>
      <CameraFeed id={id} info={info} fieldOfView={fieldOfView} />
    </Panel>
  );
}

function CameraFeed({
  id,
  info,
  fieldOfView,
}: {
  id: string;
  info?: CameraInfo;
  fieldOfView?: number;
}) {
  const { status, clientId } = useCore();
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const [zoom, setZoom] = useState(1);

  // The mouse wheel zooms the picture about its center (where the crosshair is). The
  // listener is not passive, so the wheel zooms instead of scrolling the sidebar; it is
  // attached whenever the live view appears.
  const view = useCallback((el: HTMLDivElement | null) => {
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY === 0) return;
      e.preventDefault();
      setZoom((z) => {
        const next = e.deltaY < 0 ? z * ZOOM_STEP : z / ZOOM_STEP;
        return Math.min(MAX_ZOOM, Math.max(1, Math.abs(next - 1) < 0.01 ? 1 : next));
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

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
    <div
      ref={view}
      className="camera-view camera-live"
      style={{ aspectRatio: aspect }}
      title="Scroll to zoom"
    >
      {!failed && (
        <div className="camera-zoom" style={{ transform: `scale(${zoom})` }}>
          <StreamImage src={src} alt={`${id} camera`} onError={() => setFailed(true)} />
        </div>
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
      {zoom > 1 && (
        <button
          type="button"
          className="camera-zoom-reset"
          title="Back to the whole view"
          onClick={() => setZoom(1)}
        >
          {zoom.toFixed(1)}×
        </button>
      )}
      {fieldOfView ? <ScaleBar fieldOfView={fieldOfView / zoom} /> : null}
    </div>
  );
}

/** A bar of a round length in the lower right, drawn to the camera's scale. */
function ScaleBar({ fieldOfView }: { fieldOfView: number }) {
  const mm = scaleLength(fieldOfView);
  return (
    <div className="camera-scale" style={{ width: `${(mm / fieldOfView) * 100}%` }}>
      <span>{mm} mm</span>
      <div className="camera-scale-bar" />
    </div>
  );
}

/** An MJPEG stream, read with fetch so its connection closes when it goes away. */
function StreamImage({ src, alt, onError }: { src: string; alt: string; onError: () => void }) {
  const ref = useRef<HTMLImageElement>(null);
  const failed = useRef(onError);
  useEffect(() => {
    failed.current = onError;
  }, [onError]);
  useEffect(() => {
    const img = ref.current;
    if (!img) return;
    const abort = new AbortController();
    let shown: string | null = null;
    readMjpeg(src, abort.signal, (jpeg) => {
      const url = URL.createObjectURL(jpeg);
      img.src = url;
      if (shown) URL.revokeObjectURL(shown);
      shown = url;
    }).then(
      () => abort.signal.aborted || failed.current(),
      () => failed.current(),
    );
    return () => {
      abort.abort();
      if (shown) URL.revokeObjectURL(shown);
    };
  }, [src]);
  return <img ref={ref} alt={alt} draggable={false} />;
}
