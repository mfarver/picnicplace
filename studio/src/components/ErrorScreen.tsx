import { RotateCw, TriangleAlert } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { type Compatibility, checkCore } from "../api/version";

const RESTART =
  "Restart the core (for example, stop it and run make core), then reload this page.";

/** Shown across the top when the core and this Studio come from different versions. */
export function VersionBanner() {
  return (
    <div className="version-banner" role="alert">
      <TriangleAlert size={16} aria-hidden />
      <span>
        This Studio and the core come from different versions of PicnicPlace, so parts of the
        Studio may not work. {RESTART}
      </span>
      <button type="button" className="button" onClick={() => window.location.reload()}>
        <RotateCw size={14} aria-hidden />
        Reload
      </button>
    </div>
  );
}

interface State {
  error: Error | null;
  compatibility: Compatibility;
}

/**
 * Catches a crash anywhere in the Studio and says what happened, instead of leaving a
 * blank page. The usual cause is a core from another version than the Studio, so it
 * checks for that and says so.
 */
export class ErrorScreen extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, compatibility: "unknown" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("The Studio stopped:", error, info.componentStack);
    void checkCore().then((compatibility) => this.setState({ compatibility }));
  }

  render() {
    const { error, compatibility } = this.state;
    if (!error) return this.props.children;
    return (
      <main className="crash" role="alert">
        <div className="crash-box">
          <h1>
            <TriangleAlert size={22} aria-hidden />
            The Studio stopped
          </h1>
          {compatibility === "different" ? (
            <p>
              The core is from a different version of PicnicPlace than this Studio. {RESTART}
            </p>
          ) : (
            <p>
              Something went wrong while showing this page. Reloading usually helps. If you
              updated PicnicPlace recently, the core may still be running the old version:{" "}
              {RESTART.charAt(0).toLowerCase() + RESTART.slice(1)}
            </p>
          )}
          <pre className="crash-detail">{error.message || String(error)}</pre>
          <button type="button" className="button primary" onClick={() => window.location.reload()}>
            <RotateCw size={16} aria-hidden />
            Reload
          </button>
        </div>
      </main>
    );
  }
}
