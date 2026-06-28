"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getBackendUrl, saveSession } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${getBackendUrl()}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Login failed");

      saveSession(data.access_token, data.user);
      if (data.user.role === "super_admin") {
        router.push("/super-admin");
      } else {
        router.push("/dashboard");
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#0A0E1A] flex items-center justify-center px-4">
      <div className="w-full max-w-md rounded-2xl border border-[#1F293D] bg-[#111726] p-8 shadow-xl">
        <Link href="/" className="text-sky-400 text-sm hover:underline">
          ← Back to home
        </Link>
        <h1 className="text-2xl font-bold text-white mt-4 mb-1">Sign in</h1>
        <p className="text-sm text-slate-400 mb-8">Access your Alpha agent console</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-xs font-bold text-slate-400 uppercase tracking-wider">Email</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-lg bg-[#1A2234] border border-[#2D3D54] px-3 py-2.5 text-sm text-white focus:border-sky-500 focus:outline-none"
              placeholder="you@company.com"
            />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-400 uppercase tracking-wider">Password</label>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-lg bg-[#1A2234] border border-[#2D3D54] px-3 py-2.5 text-sm text-white focus:border-sky-500 focus:outline-none"
            />
          </div>
          {error && <p className="text-sm text-rose-400">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-gradient-to-r from-sky-500 to-indigo-600 py-2.5 font-semibold text-white disabled:opacity-50"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="mt-6 text-center text-sm text-slate-400">
          No account?{" "}
          <Link href="/register" className="text-sky-400 hover:underline">
            Register your organization
          </Link>
        </p>
      </div>
    </div>
  );
}
