import type { ClientInfo } from "./api/types";

const ROTARY_AXES = new Set(["A", "B", "C"]);

/** Format an axis position: millimeters to 3 places, rotation in degrees to 2. */
export function formatAxis(axis: string, value: number | undefined): string {
  if (value === undefined) return "—";
  return ROTARY_AXES.has(axis) ? `${withinTurn(value).toFixed(2)}°` : value.toFixed(3);
}

/** An angle within one turn, -180 to 180 degrees: a nozzle on a rotating coupling (as a
 * LumenPnP's) turns on without winding back, so its axis's position counts whole turns. */
export function withinTurn(degrees: number): number {
  const a = (((degrees + 180) % 360) + 360) % 360 - 180;
  return a === -180 ? 180 : a + 0;
}

/** A client as people see it: its name, and where it connected from. */
export function describeClient(client: ClientInfo): string {
  return client.address ? `${client.name} at ${client.address}` : client.name;
}
