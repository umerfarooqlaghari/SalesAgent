import React, { useCallback, useEffect, useState } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import AdminIntegrations from "@/app/components/AdminIntegrations";

/**
 * X11 / F01: the dashboard re-renders on every streamed WebSocket token. When
 * `getHeaders` was a new closure per render, that invalidated the panel's
 * `load` callback, re-fetched /api/admin/tenant and overwrote whatever the
 * operator was typing. These tests re-create that pressure.
 */

const TENANT_PAYLOAD = {
  tenant_id: "t_test",
  org_name: "Test Co",
  integrations: {
    inventory: { enabled: true, sources: [] },
    crm: { enabled: false, provider: "none", config: {} },
    calendar: { enabled: false, provider: "none", config: {} },
  },
  settings: { company_description: "", system_prompt: "SERVER PROMPT" },
};

const SCHEMA_PAYLOAD = {
  categories: [
    {
      id: "inventory",
      label: "Inventory",
      description: "Inventory sources",
      allow_multiple: true,
      providers: [{ id: "postgres", label: "Postgres", fields: [] }],
    },
  ],
};

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function routedFetch(overrides: Record<string, () => Response> = {}) {
  return vi.fn(async (url: unknown) => {
    const u = String(url);
    for (const [fragment, make] of Object.entries(overrides)) {
      if (u.includes(fragment)) return make();
    }
    if (u.includes("/api/admin/integration-schemas")) return okJson(SCHEMA_PAYLOAD);
    if (u.includes("/api/admin/tenant")) return okJson(TENANT_PAYLOAD);
    return okJson({});
  });
}

/**
 * Mirrors the dashboard under load: a parent that re-renders constantly.
 * `stableHeaders: false` re-creates the exact regression — an inline callback
 * prop — which is what X11 is meant to catch if someone reintroduces it.
 */
function StreamingParent({ ticks, stableHeaders }: { ticks: number; stableHeaders: boolean }) {
  const [n, setN] = useState(0);
  const stable = useCallback(() => ({ Authorization: "Bearer test" }), []);
  const getHeaders = stableHeaders ? stable : () => ({ Authorization: "Bearer test" });

  useEffect(() => {
    if (n < ticks) {
      const t = setTimeout(() => setN((v) => v + 1), 1);
      return () => clearTimeout(t);
    }
  }, [n, ticks]);

  return (
    <div>
      <span data-testid="tick">{n}</span>
      <AdminIntegrations backendUrl="http://backend.local" getHeaders={getHeaders} />
    </div>
  );
}

beforeEach(() => {
  vi.stubGlobal("sessionStorage", {
    length: 0,
    key: vi.fn(() => null),
    setItem: vi.fn(),
    getItem: vi.fn(),
    removeItem: vi.fn(),
  } as unknown as Storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AdminIntegrations — re-render pressure (F01 / X11)", () => {
  it.each([
    ["a stable getHeaders", true],
    ["an inline getHeaders (the original regression)", false],
  ])("survives %s while the parent re-renders", async (_label, stableHeaders) => {
    const fetchMock = routedFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(<StreamingParent ticks={12} stableHeaders={stableHeaders as boolean} />);

    const textarea = await screen.findByDisplayValue("SERVER PROMPT");

    fireEvent.change(textarea, { target: { value: "MY UNSAVED PROMPT" } });

    // Let the parent finish churning.
    await waitFor(() => expect(screen.getByTestId("tick").textContent).toBe("12"));

    // The typed value must survive every one of those renders.
    expect((textarea as HTMLTextAreaElement).value).toBe("MY UNSAVED PROMPT");

    // And the tenant record must have been fetched exactly once — a second
    // fetch is what used to overwrite the form.
    const tenantCalls = fetchMock.mock.calls.filter((c) =>
      String(c[0]).includes("/api/admin/tenant")
    );
    expect(tenantCalls).toHaveLength(1);
  });
});

describe("dashboard wiring (F01 source guard)", () => {
  it("keeps getHeaders memoised in dashboard/page.tsx", async () => {
    const fs = await import("node:fs/promises");
    const path = await import("node:path");
    const src = await fs.readFile(
      path.join(process.cwd(), "app/dashboard/page.tsx"),
      "utf8"
    );
    // Strip comments so the assertion cannot be satisfied by prose.
    const code = src
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split("\n")
      .filter((l) => !l.trim().startsWith("//"))
      .join("\n");
    expect(code).toMatch(/const\s+getHeaders\s*=\s*useCallback\(/);
    expect(code).not.toMatch(/const\s+getHeaders\s*=\s*\(\)\s*=>\s*authHeaders\(\)\s*;/);
  });
});

describe("AdminIntegrations — save() must not clobber the prompt (F05)", () => {
  it("keeps the operator's unsaved prompt when an integration save returns one", async () => {
    const fetchMock = routedFetch({
      "/api/admin/integrations": () =>
        okJson({
          integrations: TENANT_PAYLOAD.integrations,
          settings: { system_prompt: "SERVER PROMPT" },
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const getHeaders = () => ({ Authorization: "Bearer test" });
    render(<AdminIntegrations backendUrl="http://backend.local" getHeaders={getHeaders} />);

    const textarea = await screen.findByDisplayValue("SERVER PROMPT");
    fireEvent.change(textarea, { target: { value: "HALF-WRITTEN PROMPT" } });

    const saveButtons = screen.getAllByRole("button", { name: /save changes/i });
    await act(async () => {
      fireEvent.click(saveButtons[0]);
    });

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith("/api/admin/integrations"))).toBe(
        true
      )
    );

    expect((textarea as HTMLTextAreaElement).value).toBe("HALF-WRITTEN PROMPT");
  });

  it("accepts the server prompt when there is nothing unsaved to lose", async () => {
    const fetchMock = routedFetch({
      "/api/admin/integrations": () =>
        okJson({
          integrations: TENANT_PAYLOAD.integrations,
          settings: { system_prompt: "REGENERATED BY SERVER" },
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AdminIntegrations
        backendUrl="http://backend.local"
        getHeaders={() => ({ Authorization: "Bearer test" })}
      />
    );

    await screen.findByDisplayValue("SERVER PROMPT");
    const saveButtons = screen.getAllByRole("button", { name: /save changes/i });
    await act(async () => {
      fireEvent.click(saveButtons[0]);
    });

    await screen.findByDisplayValue("REGENERATED BY SERVER");
  });
});

describe("AdminIntegrations — load failure is recoverable (F06)", () => {
  it("shows the reason and a Retry instead of a permanent 'Loading…'", async () => {
    let attempt = 0;
    const fetchMock = vi.fn(async (url: unknown) => {
      const u = String(url);
      if (u.includes("/api/admin/tenant")) {
        attempt += 1;
        if (attempt === 1) {
          return { ok: false, status: 503, json: async () => ({}) } as unknown as Response;
        }
        return okJson(TENANT_PAYLOAD);
      }
      return okJson(SCHEMA_PAYLOAD);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AdminIntegrations
        backendUrl="http://backend.local"
        getHeaders={() => ({ Authorization: "Bearer test" })}
      />
    );

    const retry = await screen.findByRole("button", { name: /^retry$/i });
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(retry);
    });

    await screen.findByDisplayValue("SERVER PROMPT");
  });
});

describe("AdminIntegrations — missing provider schemas (F18)", () => {
  it("disables 'Add inventory source' instead of throwing on a non-null assertion", async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      const u = String(url);
      if (u.includes("/api/admin/integration-schemas")) {
        return { ok: false, status: 500, json: async () => ({}) } as unknown as Response;
      }
      return okJson(TENANT_PAYLOAD);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AdminIntegrations
        backendUrl="http://backend.local"
        getHeaders={() => ({ Authorization: "Bearer test" })}
      />
    );

    const addButton = await screen.findByRole("button", { name: /add inventory source/i });
    expect(addButton).toBeDisabled();

    // Clicking a disabled button is a no-op, but assert the tree survives it.
    fireEvent.click(addButton);
    expect(screen.getByText(/Provider definitions didn't load/i)).toBeInTheDocument();
  });
});
