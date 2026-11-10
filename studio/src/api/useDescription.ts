import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { MachineProfile } from "./types";

const listeners = new Set<(description: MachineProfile) => void>();

/** Show a changed machine description (such as a new discard location) everywhere. */
export function publishDescription(description: MachineProfile): void {
  for (const listener of listeners) listener(description);
}

/**
 * The machine description (profile). Fetched each time the event socket connects, since
 * the core may have restarted with a different profile, and updated when this Studio
 * changes it.
 */
export function useDescription(): MachineProfile | undefined {
  const { status, clientId } = useCore();
  const [description, setDescription] = useState<MachineProfile>();

  useEffect(() => {
    listeners.add(setDescription);
    return () => {
      listeners.delete(setDescription);
    };
  }, []);

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
