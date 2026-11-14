import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { SetupState } from "./types";

/** The setup wizard's choices, what the core runs, and what the config file pins. */
export function useSetup(): [SetupState | null, (s: SetupState) => void] {
  const { status, clientId } = useCore();
  const [state, setState] = useState<SetupState | null>(null);
  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/setup").then(({ data }) => {
      if (!cancelled && data) setState(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, clientId]);
  return [state, setState];
}
