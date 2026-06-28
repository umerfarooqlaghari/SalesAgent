"use client";

import Link from "next/link";

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-[#0A0E1A] text-white">
      <header className="border-b border-[#1F293D] px-6 py-4 flex items-center justify-between max-w-6xl mx-auto w-full">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-sky-500 to-indigo-600 flex items-center justify-center font-bold text-sm">
            α
          </div>
          <span className="font-extrabold tracking-wide">ALPHA</span>
        </div>
        <nav className="flex items-center gap-3 text-sm">
          <Link href="/login" className="text-slate-300 hover:text-white px-3 py-2">
            Sign in
          </Link>
          <Link
            href="/register"
            className="rounded-lg bg-gradient-to-r from-sky-500 to-indigo-600 px-4 py-2 font-semibold text-white hover:opacity-90"
          >
            Get started
          </Link>
        </nav>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-20">
        <div className="max-w-3xl">
          <p className="text-sky-400 text-sm font-bold uppercase tracking-widest mb-4">
            AI Sales Agent Platform
          </p>
          <h1 className="text-4xl sm:text-5xl font-extrabold leading-tight mb-6">
            Voice + chat agents that sell, book, and support — per client, fully isolated.
          </h1>
          <p className="text-lg text-slate-400 mb-10 leading-relaxed">
            Register your organization, connect Shopify or your database, and launch an autonomous SDR
            with real-time supervisor handoff, orders, and appointments.
          </p>
          <div className="flex flex-wrap gap-4">
            <Link
              href="/register"
              className="rounded-xl bg-gradient-to-r from-sky-500 to-indigo-600 px-6 py-3 font-semibold shadow-lg shadow-indigo-600/20"
            >
              Create free account
            </Link>
            <Link
              href="/login"
              className="rounded-xl border border-slate-600 px-6 py-3 font-semibold text-slate-200 hover:bg-slate-800/50"
            >
              Sign in to console
            </Link>
          </div>
        </div>

        <div className="grid sm:grid-cols-3 gap-6 mt-20">
          {[
            {
              title: "Multi-tenant",
              desc: "Each client gets isolated data, API keys, and integration configs.",
            },
            {
              title: "Voice + chat",
              desc: "Vapi voice calls linked to typed chat for accurate contact capture.",
            },
            {
              title: "Your stack",
              desc: "Shopify, Postgres, SQL Server — connect via admin, no code changes.",
            },
          ].map((f) => (
            <div key={f.title} className="rounded-xl border border-[#1F293D] bg-[#111726] p-6">
              <h3 className="font-bold text-white mb-2">{f.title}</h3>
              <p className="text-sm text-slate-400">{f.desc}</p>
            </div>
          ))}
        </div>
      </main>

      <footer className="border-t border-[#1F293D] py-8 text-center text-xs text-slate-500">
        Alpha Sales Agent · B2B SDR Platform
      </footer>
    </div>
  );
}
