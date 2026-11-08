import { type ReactNode, useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { reconnectDelayMs } from "./backoff";
import { eventsUrl, setClientId } from "./client";
import { CoreContext, coreReducer, initialCoreState } from "./core";
import { loadToken, saveToken } from "./token";
import type { ClientMessage, ServerEvent } from "./types";

const HEARTBEAT_MS = 2000;
const CLOSE_UNAUTHORIZED = 4401;

/**
 * Holds the event-socket connection to the core. While this socket is open, this
 * client counts as present, which the core requires before it allows motion.
 */
export function CoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(coreReducer, initialCoreState);
  const [token, setTokenState] = useState(loadToken);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let heartbeat: number | undefined;
    let retry: number | undefined;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      socket = new WebSocket(eventsUrl(token));
      socket.onopen = () => {
        attempt = 0;
        const ping: ClientMessage = { type: "ping" };
        heartbeat = window.setInterval(() => socket?.send(JSON.stringify(ping)), HEARTBEAT_MS);
      };
      socket.onmessage = (msg: MessageEvent<string>) => {
        const event = JSON.parse(msg.data) as ServerEvent;
        if (event.type === "hello") setClientId(event.client_id);
        dispatch({ kind: "event", event });
      };
      socket.onclose = (ev) => {
        window.clearInterval(heartbeat);
        setClientId(null);
        if (stopped) return;
        if (ev.code === CLOSE_UNAUTHORIZED) {
          dispatch({ kind: "status", status: "unauthorized" });
          return;
        }
        dispatch({ kind: "status", status: "reconnecting" });
        retry = window.setTimeout(connect, reconnectDelayMs(attempt++));
      };
    };

    dispatch({ kind: "status", status: "connecting" });
    connect();
    return () => {
      stopped = true;
      window.clearInterval(heartbeat);
      window.clearTimeout(retry);
      socket?.close();
    };
  }, [token]);

  const setToken = useCallback((next: string) => {
    saveToken(next);
    setTokenState(next);
  }, []);

  const value = useMemo(() => ({ ...state, setToken }), [state, setToken]);
  return <CoreContext.Provider value={value}>{children}</CoreContext.Provider>;
}
