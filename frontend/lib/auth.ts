export function getBackendUrl(): string {
  const localDefault = "http://127.0.0.1:8765";
  const envUrl = process.env.NEXT_PUBLIC_BACKEND_URL;

  if (typeof window === "undefined") {
    return envUrl || localDefault;
  }

  const isLocalHost =
    window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1";

  if (isLocalHost) {
    const stored = localStorage.getItem("sdr_backend_url");
    if (stored && (stored.includes("127.0.0.1") || stored.includes("localhost"))) {
      return stored;
    }
    return localDefault;
  }

  return localStorage.getItem("sdr_backend_url") || envUrl || localDefault;
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
  if (typeof window === "undefined") return null;
  return localStorage.getItem(API_KEY_KEY);
}

export function saveSession(accessToken: string, user: AuthUser, apiKey?: string) {
  localStorage.setItem(TOKEN_KEY, accessToken);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
  if (apiKey) localStorage.setItem(API_KEY_KEY, apiKey);
}

export function saveApiKey(apiKey: string) {
  localStorage.setItem(API_KEY_KEY, apiKey);
}

export function clearSession() {
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

export async function fetchMe(backendUrl?: string): Promise<AuthUser | null> {
  const token = getAccessToken();
  if (!token) return null;
  const base = backendUrl || getBackendUrl();
  const res = await fetch(`${base}/api/auth/me`, { headers: authHeaders() });
  if (!res.ok) return null;
  return res.json();
}
