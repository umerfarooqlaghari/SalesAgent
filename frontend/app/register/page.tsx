"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getBackendUrl, saveApiKey, saveSession } from "@/lib/auth";

export default function RegisterPage() {
  const router = useRouter();
  const [orgName, setOrgName] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${getBackendUrl()}/api/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ org_name: orgName, name, email, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Registration failed");

      saveSession(data.access_token, data.user, data.api_key);
      setApiKey(data.api_key);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Registration failed");
    } finally {
      setLoading(false);
    }
  };

  const copyKey = () => {
    if (apiKey) {
      navigator.clipboard.writeText(apiKey);
      setCopied(true);
    }
  };

  if (apiKey) {
    return (
      <div className="min-h-screen bg-[#0A0E1A] flex items-center justify-center px-4">
        <div className="w-full max-w-lg rounded-2xl border border-emerald-500/30 bg-[#111726] p-8">
          <h1 className="text-xl font-bold text-emerald-400 mb-2">Account created</h1>
          <p className="text-sm text-slate-400 mb-6">
            Copy your API key now. It powers voice agents and machine access — we cannot show it again.
          </p>
          <div className="rounded-lg bg-[#0A0E1A] border border-[#2D3D54] p-4 font-mono text-xs text-sky-300 break-all">
            {apiKey}
          </div>
          <div className="flex gap-3 mt-6">
            <button
              onClick={copyKey}
              className="flex-1 rounded-lg border border-sky-600 text-sky-400 py-2 text-sm font-semibold"
            >
              {copied ? "Copied!" : "Copy API key"}
            </button>
            <button
              onClick={() => router.push("/dashboard")}
              className="flex-1 rounded-lg bg-gradient-to-r from-sky-500 to-indigo-600 py-2 text-sm font-semibold text-white"
            >
              Go to dashboard
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#0A0E1A] flex items-center justify-center px-4 py-12">
      <div className="w-full max-w-md rounded-2xl border border-[#1F293D] bg-[#111726] p-8 shadow-xl">
        <Link href="/" className="text-sky-400 text-sm hover:underline">
          ← Back to home
        </Link>
        <h1 className="text-2xl font-bold text-white mt-4 mb-1">Create account</h1>
        <p className="text-sm text-slate-400 mb-8">Register your organization and get an API key</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-xs font-bold text-slate-400 uppercase tracking-wider">Organization</label>
            <input
              required
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              className="mt-1 w-full rounded-lg bg-[#1A2234] border border-[#2D3D54] px-3 py-2.5 text-sm text-white focus:border-sky-500 focus:outline-none"
              placeholder="Acme Corp"
            />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-400 uppercase tracking-wider">Your name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded-lg bg-[#1A2234] border border-[#2D3D54] px-3 py-2.5 text-sm text-white focus:border-sky-500 focus:outline-none"
              placeholder="Jane Smith"
            />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-400 uppercase tracking-wider">Work email</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-lg bg-[#1A2234] border border-[#2D3D54] px-3 py-2.5 text-sm text-white focus:border-sky-500 focus:outline-none"
            />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-400 uppercase tracking-wider">Password</label>
            <input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-lg bg-[#1A2234] border border-[#2D3D54] px-3 py-2.5 text-sm text-white focus:border-sky-500 focus:outline-none"
            />
            <p className="text-[10px] text-slate-500 mt-1">Minimum 8 characters</p>
          </div>
          {error && <p className="text-sm text-rose-400">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-gradient-to-r from-sky-500 to-indigo-600 py-2.5 font-semibold text-white disabled:opacity-50"
          >
            {loading ? "Creating…" : "Create account"}
          </button>
        </form>

        <p className="mt-6 text-center text-sm text-slate-400">
          Already have an account?{" "}
          <Link href="/login" className="text-sky-400 hover:underline">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
