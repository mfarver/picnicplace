import { createContext, useContext } from "react";
import type { LeaseState, MachineState, Operation, PresenceState, ServerEvent } from "./types";

export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "unauthorized";

export interface CoreState {
  status: ConnectionStatus;
  clientId?: string;
  serverVersion?: string;
  machine?: MachineState;
  presence?: PresenceState;
  lease?: LeaseState;
  operations: Record<string, Operation>;
}

/** Recent operations kept for display; older ones are dropped. */
const MAX_OPERATIONS = 50;

export const initialCoreState: CoreState = { status: "connecting", operations: {} };

export type CoreAction =
  | { kind: "status"; status: ConnectionStatus }
  | { kind: "event"; event: ServerEvent };

export function coreReducer(state: CoreState, action: CoreAction): CoreState {
  if (action.kind === "status") return { ...state, status: action.status };
  const event = action.event;
  switch (event.type) {
    case "hello":
      return {
        ...state,
        status: "open",
        clientId: event.client_id,
        serverVersion: event.server_version,
        machine: event.machine,
        presence: event.presence,
        lease: event.lease,
      };
    case "machine.state":
      return { ...state, machine: event.machine };
    case "presence":
      return { ...state, presence: event.presence };
    case "lease":
      return { ...state, lease: event.lease };
    case "operation": {
      const operations = { ...state.operations, [event.operation.id]: event.operation };
      const ids = Object.keys(operations);
      for (const id of ids.slice(0, Math.max(0, ids.length - MAX_OPERATIONS))) {
        delete operations[id];
      }
      return { ...state, operations };
    }
    default:
      return state;
  }
}

export interface CoreContextValue extends CoreState {
  setToken: (token: string) => void;
}

export const CoreContext = createContext<CoreContextValue | null>(null);

/** Whether this client holds the control lease. */
export function hasControl(state: CoreState): boolean {
  return !!state.clientId && state.lease?.holder?.id === state.clientId;
}

export function useCore(): CoreContextValue {
  const ctx = useContext(CoreContext);
  if (!ctx) throw new Error("useCore must be used inside <CoreProvider>");
  return ctx;
}
