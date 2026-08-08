import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * F04: a stored `sdr_backend_url` used to be trusted unconditionally off
 * localhost — one XSS could permanently redirect every Bearer token, WebSocket
 * ticket and plaintext database password to an attacker's host.
 *
 * F03: `fetchMe` collapsed "rejected" and "unreachable" into `null`, and the
 * dashboard answered `null` by clearing the session.
 *
 * F11: the `sk_live_` secret key was persisted in localStorage.
 */

const store = new Map<string, string>();

function stubLocalStorage() {
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    get length() {
      return store.size;
    },
    key: (i: number) => Array.from(store.keys())[i] ?? null,
  } as unknown as Storage);
}

function stubLocation(href: string) {
  const url = new URL(href);
  vi.stubGlobal("window", {
    location: {
      hostname: url.hostname,
      origin: url.origin,
      protocol: url.protocol,
    },
  });
}

async function freshAuth() {
  vi.resetModules();
  return import("@/lib/auth");
}

beforeEach(() => {
  store.clear();
  stubLocalStorage();
  vi.stubGlobal("document", { cookie: "" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("getBackendUrl — stored-origin override (F04)", () => {
  it("ignores an attacker-controlled origin in production", async () => {
    stubLocation("https://console.example.com/dashboard");
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com");
    store.set("sdr_backend_url", "https://evil.attacker.tld");

    const { getBackendUrl, isAllowedBackendUrl } = await freshAuth();

    expect(isAllowedBackendUrl("https://evil.attacker.tld")).toBe(false);
    expect(getBackendUrl()).toBe("https://api.example.com");
  });

  it("still honours the configured backend origin", async () => {
    stubLocation("https://console.example.com/dashboard");
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com");
    store.set("sdr_backend_url", "https://api.example.com/v2");

    const { getBackendUrl } = await freshAuth();
    expect(getBackendUrl()).toBe("https://api.example.com/v2");
  });

  it("honours an explicitly allowlisted origin", async () => {
    stubLocation("https://console.example.com/dashboard");
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com");
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL_ALLOWLIST", "https://staging-api.example.com");
    store.set("sdr_backend_url", "https://staging-api.example.com");

    const { getBackendUrl } = await freshAuth();
    expect(getBackendUrl()).toBe("https://staging-api.example.com");
  });

  it("keeps the permissive local-development behaviour", async () => {
    stubLocation("http://localhost:3000/dashboard");
    store.set("sdr_backend_url", "http://127.0.0.1:9999");

    const { getBackendUrl } = await freshAuth();
    expect(getBackendUrl()).toBe("http://127.0.0.1:9999");
  });
});

describe("fetchMeResult — rejected vs unreachable (F03)", () => {
  beforeEach(() => {
    stubLocation("https://console.example.com/dashboard");
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com");
    store.set("alpha_access_token", "tok");
  });

  it("reports a 401 as unauthorized", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));
    const { fetchMeResult } = await freshAuth();
    expect((await fetchMeResult()).status).toBe("unauthorized");
  });

  it("reports a network failure as unreachable, not a rejected session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const { fetchMeResult } = await freshAuth();
    const result = await fetchMeResult();
    expect(result.status).toBe("unreachable");
    expect(result.status === "unreachable" && result.error).toMatch(/Failed to fetch/);
  });

  it("reports a 502 as unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 502 }));
    const { fetchMeResult } = await freshAuth();
    expect((await fetchMeResult()).status).toBe("unreachable");
  });

  it("returns the user on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ user_id: "u1", email: "a@b.c", name: "A", role: "tenant_admin" }),
      })
    );
    const { fetchMeResult } = await freshAuth();
    const result = await fetchMeResult();
    expect(result.status).toBe("ok");
    expect(result.status === "ok" && result.user.email).toBe("a@b.c");
  });

  it("reports a missing token as unauthenticated without a request", async () => {
    store.delete("alpha_access_token");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { fetchMeResult } = await freshAuth();
    expect((await fetchMeResult()).status).toBe("unauthenticated");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("secret API key persistence (F11)", () => {
  beforeEach(() => stubLocation("https://console.example.com/dashboard"));

  it("never writes sk_live_ to localStorage in production", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const { saveApiKey, getStoredApiKey } = await freshAuth();

    saveApiKey("sk_live_supersecret");

    expect(store.get("sdr_api_key")).toBeUndefined();
    // Still usable for the life of the page.
    expect(getStoredApiKey()).toBe("sk_live_supersecret");
  });

  it("sweeps a key written by an older build", async () => {
    vi.stubEnv("NODE_ENV", "production");
    store.set("sdr_api_key", "sk_live_leftover");
    const { saveApiKey } = await freshAuth();

    saveApiKey("sk_live_new");
    expect(store.get("sdr_api_key")).toBeUndefined();
  });
});

describe("session hint cookie (F09)", () => {
  it("writes the role on login and clears it on sign-out", async () => {
    stubLocation("https://console.example.com/login");
    const cookieJar = { cookie: "" };
    vi.stubGlobal("document", cookieJar);

    const { saveSession, clearSession, SESSION_HINT_COOKIE } = await freshAuth();

    saveSession("tok", {
      user_id: "u1",
      email: "a@b.c",
      name: "A",
      role: "super_admin",
      tenant_id: null,
    });
    expect(cookieJar.cookie).toContain(`${SESSION_HINT_COOKIE}=super_admin`);
    expect(cookieJar.cookie).toContain("Secure");

    clearSession();
    expect(cookieJar.cookie).toContain("max-age=0");
  });
});
