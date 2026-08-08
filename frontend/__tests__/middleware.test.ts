import { describe, it, expect } from "vitest";
import { middleware } from "@/middleware";
import { NextRequest } from "next/server";

/**
 * F09: before middleware.ts existed, /dashboard and /super-admin were guarded
 * only by a useEffect that ran after the bundle had already been served — the
 * whole super-admin surface was downloadable unauthenticated.
 *
 * The cookie is a presence hint, not a credential (see lib/auth.ts). These
 * tests pin the routing behaviour, not an authorisation guarantee.
 */

function request(path: string, cookie?: string) {
  const req = new NextRequest(new URL(`https://console.example.com${path}`));
  if (cookie) req.cookies.set("sdr_session", cookie);
  return req;
}

describe("middleware route gate (F09)", () => {
  it("bounces an anonymous visitor off /super-admin", () => {
    const res = middleware(request("/super-admin"));
    expect(res.status).toBe(307);
    const location = new URL(res.headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/super-admin");
  });

  it("bounces an anonymous visitor off /dashboard", () => {
    const res = middleware(request("/dashboard/anything"));
    const location = new URL(res.headers.get("location")!);
    expect(location.pathname).toBe("/login");
  });

  it("sends a tenant admin away from /super-admin", () => {
    const res = middleware(request("/super-admin", "tenant_admin"));
    const location = new URL(res.headers.get("location")!);
    expect(location.pathname).toBe("/dashboard");
  });

  it("lets a super admin through", () => {
    const res = middleware(request("/super-admin", "super_admin"));
    expect(res.headers.get("location")).toBeNull();
  });

  it("lets a tenant admin into the dashboard", () => {
    const res = middleware(request("/dashboard", "tenant_admin"));
    expect(res.headers.get("location")).toBeNull();
  });

  it("leaves public routes alone", () => {
    expect(middleware(request("/")).headers.get("location")).toBeNull();
    expect(middleware(request("/login")).headers.get("location")).toBeNull();
  });
});
