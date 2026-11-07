import type { components } from "./schema";

type Schemas = components["schemas"];

export type MachineState = Schemas["MachineState"];
export type PresenceState = Schemas["PresenceState"];
export type Operation = Schemas["Operation"];
export type ServerEvent = Schemas["ServerEvent"];
export type ClientMessage = Schemas["ClientMessage"];
export type ErrorResponse = Schemas["ErrorResponse"];
export type MachineProfile = Schemas["MachineProfile"];
export type Nozzle = Schemas["Nozzle"];
export type Camera = Schemas["Camera"];
export type MachineInput = Schemas["Input"];
export type MachineOutput = Schemas["Output"];
export type FeederBank = Schemas["FeederBank"];
export type Point = Schemas["Point"];
export type Rect = Schemas["Rect"];
