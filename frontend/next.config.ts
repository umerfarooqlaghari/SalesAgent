import type { NextConfig } from "next";

/**
 * F23: the config was empty. With a session token in localStorage there was
 * nothing standing between an injected script and both the token and the
 * stored backend origin (F04). These headers are the cheap half of that
 * defence — the expensive half is moving the session to an httpOnly cookie.
 *
 * `connect-src` has to allow the API origin the browser actually talks to, so
 * it is derived from NEXT_PUBLIC_BACKEND_URL plus anything in
 * NEXT_PUBLIC_BACKEND_URL_ALLOWLIST, and it keeps wss: for the chat socket and
 * the Vapi/Daily endpoints the voice widget needs.
 */
function connectSources(): string {
  const sources = new Set<string>(["'self'", "https:", "wss:"]);
  const configured = [
    process.env.NEXT_PUBLIC_BACKEND_URL || "",
    ...(process.env.NEXT_PUBLIC_BACKEND_URL_ALLOWLIST || "").split(","),
  ];
  for (const entry of configured) {
    const trimmed = entry.trim();
    if (!trimmed) continue;
    try {
      sources.add(new URL(trimmed).origin);
    } catch {
      /* ignore malformed entries */
    }
  }
  return Array.from(sources).join(" ");
}

const isDev = process.env.NODE_ENV !== "production";

const csp = [
  "default-src 'self'",
  // Next.js injects inline bootstrap scripts; dev additionally needs eval.
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  `connect-src ${connectSources()}`,
  "media-src 'self' blob:",
  "worker-src 'self' blob:",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), geolocation=(), microphone=(self)" },
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains; preload",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
