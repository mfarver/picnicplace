import type { ClientInfo } from "./api/types";

const ROTARY_AXES = new Set(["A", "B", "C"]);

/** Format an axis position: millimeters to 3 places, rotation in degrees to 2. */
export function formatAxis(axis: string, value: number | undefined): string {
  if (value === undefined) return "—";
  return ROTARY_AXES.has(axis) ? `${value.toFixed(2)}°` : value.toFixed(3);
}

/** A client as people see it: its name, and where it connected from. */
export function describeClient(client: ClientInfo): string {
  return client.address ? `${client.name} at ${client.address}` : client.name;
}
