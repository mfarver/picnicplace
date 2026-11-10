import { useCallback, useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import type { JobLayout, RunState } from "./types";

const POLL_MS = 600;

type UseRun = [RunState | null, (state: RunState | null) => void, () => Promise<void>];

/**
 * The job run, if one has been started. Fetched when the event socket connects and when
 * an operation ends, and followed closely while the run is going.
 */
export function useRun(): UseRun {
  const { status, clientId, operations } = useCore();
  const [run, setRun] = useState<RunState | null>(null);
  const ended = Object.values(operations).filter((op) => op.status !== "running").length;
  const running = run?.status === "running";

  const refresh = useCallback(async () => {
    const { data } = await api.GET("/api/v1/run");
    if (data !== undefined) setRun(data);
  }, []);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    const fetch = () =>
      void api.GET("/api/v1/run").then(({ data }) => {
        if (!cancelled && data !== undefined) setRun(data);
      });
    fetch();
    const timer = running ? window.setInterval(fetch, POLL_MS) : undefined;
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [status, clientId, ended, running]);

  return [run, setRun, refresh];
}

/** A saved job's outline on the bed (boards, panel, and parts), for the machine map. */
export function useJobLayout(jobId: string | null | undefined, refreshKey: unknown = 0) {
  const { status } = useCore();
  const [layout, setLayout] = useState<{ jobId: string; layout: JobLayout | null } | null>(
    null,
  );
  useEffect(() => {
    if (status !== "open" || !jobId) return;
    let cancelled = false;
    void api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } } }).then(
      async ({ data }) => {
        if (!data || cancelled) return;
        const result = await api.POST("/api/v1/jobs/layout", { body: data });
        if (!cancelled) setLayout({ jobId, layout: result.data ?? null });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [status, jobId, refreshKey]);
  return layout && layout.jobId === jobId ? layout.layout : null;
}
