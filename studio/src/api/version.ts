import { useEffect, useState } from "react";
import { api } from "./client";
import { useCore } from "./core";
import { API_FINGERPRINT } from "./fingerprint";

export type Compatibility = "unknown" | "same" | "different";

/** Ask the core which API it serves, and compare it with the one this Studio expects. */
export async function checkCore(): Promise<Compatibility> {
  try {
    const { data } = await api.GET("/api/v1/health");
    if (!data) return "unknown";
    // A core older than the fingerprint sends none: also a different version.
    return data.api === API_FINGERPRINT ? "same" : "different";
  } catch {
    return "unknown";
  }
}

/** Whether the connected core is the version this Studio was built for. */
export function useCoreCompatibility(): Compatibility {
  const { status, serverVersion } = useCore();
  const [result, setResult] = useState<Compatibility>("unknown");
  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void checkCore().then((r) => {
      if (!cancelled) setResult(r);
    });
    return () => {
      cancelled = true;
    };
  }, [status, serverVersion]);
  return result;
}
