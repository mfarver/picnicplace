// The shared API token, kept per browser. Storage can be unavailable (private windows,
// blocked site data), so every access is guarded and the app works without it.

const KEY = "picnicplace.token";

let memoryToken: string | null = null;

export function loadToken(): string | null {
  try {
    return localStorage.getItem(KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

export function saveToken(token: string | null): void {
  memoryToken = token;
  try {
    if (token) localStorage.setItem(KEY, token);
    else localStorage.removeItem(KEY);
  } catch {
    // Keep the in-memory copy only.
  }
}

/** Accept `?token=...` once, save it, and remove it from the address bar. */
export function takeTokenFromUrl(): void {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  if (!token) return;
  saveToken(token);
  url.searchParams.delete("token");
  window.history.replaceState(null, "", url);
}
