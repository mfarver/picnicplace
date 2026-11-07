import { useCallback, useState } from "react";
import { errorMessage } from "./client";

interface ApiResult<T> {
  data?: T;
  error?: unknown;
}

/** Run an API call, tracking whether it is pending and the last error message. */
export function useAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async <T>(call: () => Promise<ApiResult<T>>): Promise<T | undefined> => {
    setPending(true);
    setError(null);
    try {
      const result = await call();
      if (result.error !== undefined) {
        setError(errorMessage(result.error));
        return undefined;
      }
      return result.data;
    } catch {
      setError("cannot reach the core");
      return undefined;
    } finally {
      setPending(false);
    }
  }, []);

  return { run, pending, error };
}
