import { NextResponse, type NextRequest } from "next/server";

/**
 * F09: before this file existed, /dashboard and /super-admin were protected
 * only by a `useEffect` that ran *after* the bundle had been served. An
 * unauthenticated visitor could download the whole super-admin surface, and the
 * redirect was defeated by disabling JavaScript or stopping the request.
 *
 * The edge check below reads the `sdr_session` hint cookie written by
 * `saveSession()`. Read the note on SESSION_HINT_COOKIE in lib/auth.ts: this is
 * a *hint*, not an authorisation boundary. It stops anonymous navigations at
 * the edge; the API still authenticates every request with the Bearer token,
 * and the in-page checks still run. Closing the gap properly means moving the
 * session itself into an httpOnly cookie, which needs the backend to set it.
 */
const SESSION_HINT_COOKIE = "sdr_session";

const TENANT_ROUTES = ["/dashboard"];
const SUPER_ADMIN_ROUTES = ["/super-admin"];

function matches(pathname: string, prefixes: string[]): boolean {
  return prefixes.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isTenant = matches(pathname, TENANT_ROUTES);
  const isSuperAdmin = matches(pathname, SUPER_ADMIN_ROUTES);
  if (!isTenant && !isSuperAdmin) return NextResponse.next();

  const role = request.cookies.get(SESSION_HINT_COOKIE)?.value;

  if (!role) {
    const login = request.nextUrl.clone();
    login.pathname = "/login";
    login.search = `?next=${encodeURIComponent(pathname)}`;
    return NextResponse.redirect(login);
  }

  if (isSuperAdmin && role !== "super_admin") {
    const dashboard = request.nextUrl.clone();
    dashboard.pathname = "/dashboard";
    dashboard.search = "";
    return NextResponse.redirect(dashboard);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/super-admin/:path*"],
};
