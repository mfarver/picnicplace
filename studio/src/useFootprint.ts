import { useEffect, useState } from "react";
import { api } from "./api/client";
import type { Footprint } from "./api/types";

/** Footprints fetched this session, by ID. Reload the page to see one edited since. */
const fetched = new Map<string, Footprint>();

/** A footprint from the library, once fetched: null before then, or with no such footprint
 * (or one with no pads to draw). */
export function useFootprint(id: string | null | undefined): Footprint | null {
  const [, setFetches] = useState(0);
  useEffect(() => {
    if (!id || fetched.has(id)) return;
    let cancelled = false;
    void api
      .GET("/api/v1/library/footprints/{footprint_id}", {
        params: { path: { footprint_id: id } },
      })
      .then(({ data }) => {
        if (data && data.pads.length > 0) fetched.set(id, data);
        if (!cancelled) setFetches((n) => n + 1);
      });
    return () => {
      cancelled = true;
    };
  }, [id]);
  return id ? (fetched.get(id) ?? null) : null;
}
