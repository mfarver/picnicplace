import { useEffect, useState } from "react";
import { useCore } from "../api/core";
import splashUrl from "../assets/splash.jpg";

/** Shown at least this long, so a fast local connection does not just flash it. */
const MIN_VISIBLE_MS = 900;
const FADE_MS = 300;

/**
 * Startup splash with the PicnicPlace logo. Covers the app until the first connection
 * to the core succeeds (or a token is needed), then fades out and does not return.
 */
export function Splash() {
  const { status } = useCore();
  const [minElapsed, setMinElapsed] = useState(false);
  const [gone, setGone] = useState(false);

  const ready = status === "open" || status === "unauthorized";
  const fading = ready && minElapsed;

  useEffect(() => {
    const timer = window.setTimeout(() => setMinElapsed(true), MIN_VISIBLE_MS);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!fading) return;
    const timer = window.setTimeout(() => setGone(true), FADE_MS);
    return () => window.clearTimeout(timer);
  }, [fading]);

  if (gone) return null;

  const message =
    status === "reconnecting"
      ? "Waiting for PicnicPlace Core…"
      : ready
        ? "Connected"
        : "Connecting to PicnicPlace Core…";

  return (
    <div className={`splash${fading ? " splash-fading" : ""}`} role="status" aria-live="polite">
      <img
        className="splash-logo"
        src={splashUrl}
        alt="PicnicPlace. Assemble. Picnic. Repeat."
        width={624}
        height={560}
      />
      <p className="splash-message">
        {!ready && <span className="spinner" aria-hidden />}
        {message}
      </p>
    </div>
  );
}
