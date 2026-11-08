import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import { loadToken } from "./token";

/** This Studio's ID on the event socket, from the hello message. Motion commands carry it. */
let clientId: string | null = null;

export function setClientId(id: string | null) {
  clientId = id;
}

/** Name this client gives the core, shown to other clients (for example, who has control). */
export const CLIENT_NAME = "Studio";

const auth: Middleware = {
  onRequest({ request }) {
    const token = loadToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    if (clientId) request.headers.set("X-PicnicPlace-Client", clientId);
    return request;
  },
};

/** Typed REST client, generated from the core's OpenAPI document. */
export const api = createClient<paths>({ baseUrl: window.location.origin });
api.use(auth);

export function eventsUrl(token: string | null): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${scheme}://${window.location.host}/api/v1/events`);
  if (token) url.searchParams.set("token", token);
  url.searchParams.set("name", CLIENT_NAME);
  return url.toString();
}

/**
 * URL for a media endpoint used by an <img> tag, which cannot send headers: the token
 * goes in the query string.
 */
export function mediaUrl(path: string, params: Record<string, string | number> = {}): string {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
  const token = loadToken();
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

/** Pull a readable message out of an API error body. */
export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return "invalid request";
  }
  return "request failed";
}
