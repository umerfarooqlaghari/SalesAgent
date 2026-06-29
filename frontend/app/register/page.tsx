"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getBackendUrl, saveSession } from "@/lib/auth";

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
      <div className="min-h-screen bg-gradient-to-tr from-[#FFF5FA] via-[#FAF9FC] to-[#F3F5FF] flex items-center justify-center px-4 font-sans relative">
        <div className="absolute top-0 right-0 w-[400px] h-[400px] bg-gradient-to-br from-indigo-100/30 to-transparent rounded-full blur-[100px] pointer-events-none" />
        <div className="w-full max-w-lg rounded-3xl border border-emerald-200 bg-white/95 backdrop-blur-sm p-8 shadow-xl relative z-10 space-y-6">
          <div>
            <h1 className="text-xl font-black text-emerald-700 tracking-tight">Account Created Successfully</h1>
            <p className="text-xs text-slate-500">
              Copy your platform API key below. This is required to bridge Twilio/Vapi systems and cannot be displayed again.
            </p>
          </div>
          <div className="rounded-xl bg-slate-900 border border-slate-800 p-4 font-mono text-xs text-indigo-400 break-all select-all">
            {apiKey}
          </div>
          <div className="flex gap-3">
            <button
              onClick={copyKey}
              className="flex-1 rounded-xl border border-indigo-600 text-indigo-600 hover:bg-indigo-50/50 py-3 text-xs font-bold transition-all"
            >
              {copied ? "Copied!" : "Copy API key"}
            </button>
            <button
              onClick={() => router.push("/dashboard")}
              className="flex-1 rounded-xl bg-[#4F46E5] hover:bg-[#4338CA] py-3 text-xs font-bold text-white transition-all shadow-md shadow-indigo-600/10"
            >
              Go to dashboard
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-tr from-[#FFF5FA] via-[#FAF9FC] to-[#F3F5FF] flex items-center justify-center px-4 py-12 font-sans relative">
      {/* Background blobs */}
      <div className="absolute top-0 right-0 w-[400px] h-[400px] bg-gradient-to-br from-indigo-100/30 to-transparent rounded-full blur-[100px] pointer-events-none" />
      <div className="absolute bottom-0 left-0 w-[300px] h-[300px] bg-gradient-to-tr from-purple-100/30 to-transparent rounded-full blur-[80px] pointer-events-none" />

      <div className="w-full max-w-md rounded-3xl border border-slate-200/80 bg-white/90 backdrop-blur-sm p-8 shadow-xl relative z-10 space-y-6">
        <Link href="/" className="text-indigo-600 text-xs font-bold hover:underline flex items-center gap-1">
          ← Back to home
        </Link>
        <div>
          <h1 className="text-2xl font-black text-slate-900 tracking-tight">Create account</h1>
          <p className="text-xs text-slate-400">Register your organization and obtain an API key</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Organization Name</label>
            <input
              required
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              className="mt-1.5 w-full rounded-xl bg-white border border-slate-200 px-3.5 py-3 text-xs text-slate-800 placeholder-slate-400 focus:border-[#4F46E5] focus:ring-1 focus:ring-indigo-100 focus:outline-none transition-all"
              placeholder="Acme Corp"
            />
          </div>
          <div>
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Your Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1.5 w-full rounded-xl bg-white border border-slate-200 px-3.5 py-3 text-xs text-slate-800 placeholder-slate-400 focus:border-[#4F46E5] focus:ring-1 focus:ring-indigo-100 focus:outline-none transition-all"
              placeholder="Jane Smith"
            />
          </div>
          <div>
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Work Email</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1.5 w-full rounded-xl bg-white border border-slate-200 px-3.5 py-3 text-xs text-slate-800 focus:border-[#4F46E5] focus:ring-1 focus:ring-indigo-100 focus:outline-none transition-all"
              placeholder="jane@company.com"
            />
          </div>
          <div>
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Password</label>
            <input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1.5 w-full rounded-xl bg-white border border-slate-200 px-3.5 py-3 text-xs text-slate-800 focus:border-[#4F46E5] focus:ring-1 focus:ring-indigo-100 focus:outline-none transition-all"
            />
            <p className="text-[9px] text-slate-400 mt-1">Must be at least 8 characters</p>
          </div>
          {error && <p className="text-xs font-semibold text-rose-500">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-[#4F46E5] hover:bg-[#4338CA] py-3 font-bold text-xs text-white transition-all shadow-md shadow-indigo-600/10 hover:scale-[1.01] disabled:opacity-50"
          >
            {loading ? "Creating…" : "Create account"}
          </button>
        </form>

        <p className="text-center text-xs text-slate-500">
          Already have an account?{" "}
          <Link href="/login" className="text-indigo-600 hover:underline font-bold">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
