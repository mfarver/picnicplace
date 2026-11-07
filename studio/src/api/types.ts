import type { components } from "./schema";

type Schemas = components["schemas"];

export type MachineState = Schemas["MachineState"];
export type PresenceState = Schemas["PresenceState"];
export type Operation = Schemas["Operation"];
export type ServerEvent = Schemas["ServerEvent"];
export type ClientMessage = Schemas["ClientMessage"];
export type ErrorResponse = Schemas["ErrorResponse"];
