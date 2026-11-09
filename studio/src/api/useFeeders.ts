import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { FeedersState } from "./types";

const POLL_MS = 2000;

/**
 * The feeders as the core knows them. Fetched when the event socket connects and when an
 * operation ends, and every two seconds while one runs, since picks from strips and
 * trays change how many parts they have left.
 */
export function useFeeders(): [FeedersState | null, (state: FeedersState) => void] {
  const { status, clientId, operations } = useCore();
  const [state, setState] = useState<FeedersState | null>(null);
  const ops = Object.values(operations);
  const running = ops.some((op) => op.status === "running");
  const ended = ops.filter((op) => op.status !== "running").length;

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    const fetch = () =>
      void api.GET("/api/v1/feeders").then(({ data }) => {
        if (!cancelled && data) setState(data);
      });
    fetch();
    const timer = running ? window.setInterval(fetch, POLL_MS) : undefined;
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [status, clientId, running, ended]);

  return [state, setState];
}
