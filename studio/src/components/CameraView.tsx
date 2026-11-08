import { Camera } from "lucide-react";
import { Panel } from "./Panel";

/** A camera feed in the sidebar. Live video arrives in M1; for now, a placeholder. */
export function CameraView({ id, title }: { id: string; title: string }) {
  return (
    <Panel title={title} collapseId={`camera-${id}`}>
      <div className="camera-view">
        <Camera size={28} strokeWidth={1.25} aria-hidden />
        <span>Live video arrives in M1</span>
      </div>
    </Panel>
  );
}
