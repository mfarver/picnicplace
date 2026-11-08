import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { CameraInfo } from "./types";

/** The core's cameras, fetched each time the event socket connects. */
export function useCameras(): CameraInfo[] | undefined {
  const { status, clientId } = useCore();
  const [cameras, setCameras] = useState<CameraInfo[]>();

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/cameras").then(({ data }) => {
      if (!cancelled && data) setCameras(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, clientId]);

  return cameras;
}
