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
    <div className="min-h-screen bg-gradient-to-tr from-[#FFF5FA] via-[#FAF9FC] to-[#F3F5FF] flex items-center justify-center px-4 font-sans relative">
      {/* Background blobs */}
      <div className="absolute top-0 right-0 w-[400px] h-[400px] bg-gradient-to-br from-indigo-100/30 to-transparent rounded-full blur-[100px] pointer-events-none" />
      <div className="absolute bottom-0 left-0 w-[300px] h-[300px] bg-gradient-to-tr from-purple-100/30 to-transparent rounded-full blur-[80px] pointer-events-none" />

      <div className="w-full max-w-md rounded-3xl border border-slate-200/80 bg-white/90 backdrop-blur-sm p-8 shadow-xl relative z-10 space-y-6">
        <Link href="/" className="text-indigo-600 text-xs font-bold hover:underline flex items-center gap-1">
          ← Back to home
        </Link>
        <div>
          <h1 className="text-2xl font-black text-slate-900 tracking-tight">Sign in</h1>
          <p className="text-xs text-slate-400">Access your Alpha Sales Agent console</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Email Address</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1.5 w-full rounded-xl bg-white border border-slate-200 px-3.5 py-3 text-xs text-slate-800 placeholder-slate-400 focus:border-[#4F46E5] focus:ring-1 focus:ring-indigo-100 focus:outline-none transition-all"
              placeholder="you@company.com"
            />
          </div>
          <div>
            <div className="flex justify-between items-center">
              <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Password</label>
              <Link href="/forgot-password" className="text-[10px] font-semibold text-indigo-600 hover:underline">
                Forgot?
              </Link>
            </div>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1.5 w-full rounded-xl bg-white border border-slate-200 px-3.5 py-3 text-xs text-slate-800 focus:border-[#4F46E5] focus:ring-1 focus:ring-indigo-100 focus:outline-none transition-all"
            />
          </div>
          {error && <p className="text-xs font-semibold text-rose-500">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-[#4F46E5] hover:bg-[#4338CA] py-3 font-bold text-xs text-white transition-all shadow-md shadow-indigo-600/10 hover:scale-[1.01] disabled:opacity-50"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="text-center text-xs text-slate-500">
          No account?{" "}
          <Link href="/register" className="text-indigo-600 hover:underline font-bold">
            Register your organization
          </Link>
        </p>
      </div>
    </div>
  );
}
