import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { MachineProfile } from "./types";

/**
 * The machine description (profile). Fetched each time the event socket connects, since
 * the core may have restarted with a different profile.
 */
export function useDescription(): MachineProfile | undefined {
  const { status, clientId } = useCore();
  const [description, setDescription] = useState<MachineProfile>();

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/machine/description").then(({ data }) => {
      if (!cancelled && data) setDescription(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, clientId]);

  return description;
}
