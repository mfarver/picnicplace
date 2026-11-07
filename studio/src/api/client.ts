import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import { loadToken } from "./token";

const auth: Middleware = {
  onRequest({ request }) {
    const token = loadToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
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
