import { useEffect } from "react";
import { useCameras } from "../api/useCameras";
import { useDescription } from "../api/useDescription";
import { CameraFeed } from "./CameraView";

/**
 * One camera filling its own browser window, opened from a camera view's pop-out button
 * (the Studio's address with `?camera=ID`). Big enough to watch the nozzles from afar.
 */
export function CameraWindow({ id }: { id: string }) {
  const cameras = useCameras();
  const description = useDescription();
  const info = cameras?.find((c) => c.id === id);
  const profile = description?.cameras.find((c) => c.id === id);
  const label = info?.label ?? profile?.label ?? id;

  useEffect(() => {
    document.title = `${label} · PicnicPlace`;
  }, [label]);

  // As large as the window allows at the camera's own shape.
  const ratio = info?.width && info.height ? info.width / info.height : 16 / 9;
  return (
    <div className="camera-window">
      <div className="camera-window-frame" style={{ width: `min(100vw, ${ratio * 100}vh)` }}>
        <CameraFeed
          id={id}
          info={info}
          fieldOfView={profile?.field_of_view.x}
          crosshair={!info?.monitor}
          inWindow
        />
      </div>
    </div>
  );
}
