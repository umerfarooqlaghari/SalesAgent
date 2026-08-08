const LOCAL_DEFAULT = "http://127.0.0.1:8765";

function isLocalHostname(host: string): boolean {
  return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
}

function originOf(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * F04: which backend origins a stored override is allowed to point at.
 *
 * On localhost anything local is fine. Off localhost the stored value used to
 * be trusted unconditionally, which meant a single injected script could write
 * `sdr_backend_url` once and permanently redirect every Bearer token, every
 * WebSocket ticket and every plaintext database password typed into the
 * integrations form to a host of its choosing. Now an override only survives if
 * its origin is one we already ship — the configured backend, the page's own
 * origin, or an explicit allowlist.
 */
function allowedOrigins(): Set<string> {
  const out = new Set<string>();
  const envOrigin = originOf(process.env.NEXT_PUBLIC_BACKEND_URL || "");
  if (envOrigin) out.add(envOrigin);

  const extra = process.env.NEXT_PUBLIC_BACKEND_URL_ALLOWLIST || "";
  for (const entry of extra.split(",")) {
    const origin = originOf(entry.trim());
    if (origin) out.add(origin);
  }

  if (typeof window !== "undefined") out.add(window.location.origin);
  return out;
}

export function isAllowedBackendUrl(url: string): boolean {
  const origin = originOf(url);
  if (!origin) return false;
  if (typeof window !== "undefined" && isLocalHostname(window.location.hostname)) {
    return url.includes("127.0.0.1") || url.includes("localhost");
  }
  return allowedOrigins().has(origin);
}

export function getBackendUrl(): string {
  const envUrl = process.env.NEXT_PUBLIC_BACKEND_URL;

  if (typeof window === "undefined") {
    return envUrl || LOCAL_DEFAULT;
  }

  let stored: string | null = null;
  try {
    stored = localStorage.getItem("sdr_backend_url");
  } catch {
    stored = null;
  }

  if (isLocalHostname(window.location.hostname)) {
    if (stored && (stored.includes("127.0.0.1") || stored.includes("localhost"))) {
      return stored;
    }
    return envUrl || LOCAL_DEFAULT;
  }

  if (stored && isAllowedBackendUrl(stored)) return stored;
  return envUrl || LOCAL_DEFAULT;
}

/** @deprecated prefer getBackendUrl() — evaluated once at import on server */
export const BACKEND_URL = getBackendUrl();

export type AuthUser = {
  user_id: string;
  email: string;
  name: string;
  role: "tenant_admin" | "super_admin";
  tenant_id?: string | null;
  org_name?: string | null;
};

const TOKEN_KEY = "alpha_access_token";
const USER_KEY = "alpha_user";
const API_KEY_KEY = "sdr_api_key";

/**
 * F11: `sk_live_` is a server-to-server credential. Persisting it in
 * localStorage leaves it readable by any script on the origin, forever, long
 * after the tab that needed it is gone. In production it now lives in a module
 * variable for the lifetime of the page only. Development keeps the old
 * behaviour so the console survives a reload while you are iterating.
 */
const PERSIST_SECRET_KEY = process.env.NODE_ENV !== "production";
let inMemoryApiKey: string | null = null;

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function getStoredUser(): AuthUser | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function getStoredApiKey(): string | null {
  if (inMemoryApiKey) return inMemoryApiKey;
  if (typeof window === "undefined") return null;
  if (!PERSIST_SECRET_KEY) return null;
  return localStorage.getItem(API_KEY_KEY);
}

/**
 * F09: route protection was client-only — the gate ran in a useEffect after the
 * bundle had already been served, so the entire super-admin surface was
 * downloadable unauthenticated. `middleware.ts` needs *something* readable on
 * the server, and the real token is in localStorage where the edge runtime
 * cannot see it.
 *
 * This cookie carries no credential: it is a presence-and-role hint so the edge
 * can bounce anonymous navigations before shipping the page. It is deliberately
 * not httpOnly (the client has to clear it) and it is NOT an authorisation
 * boundary — every API route still verifies the Bearer token server-side, and
 * the client-side check below still runs. Replacing this with a real httpOnly
 * session cookie is F11's remaining half and needs a backend change.
 */
export const SESSION_HINT_COOKIE = "sdr_session";

function writeSessionHint(role: AuthUser["role"] | null) {
  if (typeof document === "undefined") return;
  const secure = typeof window !== "undefined" && window.location.protocol === "https:";
  if (role) {
    document.cookie = `${SESSION_HINT_COOKIE}=${role}; path=/; max-age=86400; SameSite=Lax${
      secure ? "; Secure" : ""
    }`;
  } else {
    document.cookie = `${SESSION_HINT_COOKIE}=; path=/; max-age=0; SameSite=Lax${
      secure ? "; Secure" : ""
    }`;
  }
}

export function saveSession(accessToken: string, user: AuthUser, apiKey?: string) {
  localStorage.setItem(TOKEN_KEY, accessToken);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
  writeSessionHint(user.role);
  if (apiKey) saveApiKey(apiKey);
}

/** Keeps the edge hint in step when /api/auth/me reports a different role. */
export function refreshSessionHint(user: AuthUser | null) {
  writeSessionHint(user?.role ?? null);
}

export function saveApiKey(apiKey: string) {
  inMemoryApiKey = apiKey;
  if (typeof window === "undefined") return;
  if (PERSIST_SECRET_KEY) {
    localStorage.setItem(API_KEY_KEY, apiKey);
  } else {
    // Clear any key written by an older build that did persist it.
    localStorage.removeItem(API_KEY_KEY);
  }
}

export function clearSession() {
  inMemoryApiKey = null;
  writeSessionHint(null);
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(API_KEY_KEY);
}

export function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

/**
 * F03: the caller needs to tell "the server says this token is dead" apart from
 * "I could not reach the server". `fetchMe` collapsed both to `null`, and the
 * dashboard responded to `null` by clearing the session — so a half-typed
 * Backend URL logged you out and wiped the transcript.
 */
export type MeResult =
  | { status: "ok"; user: AuthUser }
  | { status: "unauthenticated" }
  | { status: "unauthorized" }
  | { status: "unreachable"; error: string };

export async function fetchMeResult(backendUrl?: string): Promise<MeResult> {
  const token = getAccessToken();
  if (!token) return { status: "unauthenticated" };
  const base = backendUrl || getBackendUrl();
  let res: Response;
  try {
    res = await fetch(`${base}/api/auth/me`, { headers: authHeaders() });
  } catch (e) {
    return { status: "unreachable", error: e instanceof Error ? e.message : "Network error" };
  }
  if (res.status === 401 || res.status === 403) return { status: "unauthorized" };
  if (!res.ok) return { status: "unreachable", error: `Backend returned ${res.status}` };
  try {
    return { status: "ok", user: (await res.json()) as AuthUser };
  } catch (e) {
    return { status: "unreachable", error: e instanceof Error ? e.message : "Bad response" };
  }
}

export async function fetchMe(backendUrl?: string): Promise<AuthUser | null> {
  const result = await fetchMeResult(backendUrl);
  return result.status === "ok" ? result.user : null;
}
