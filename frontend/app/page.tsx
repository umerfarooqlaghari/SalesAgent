"use client";

import Link from "next/link";

const AlphaLogo = ({ className = "w-5 h-5 text-white" }: { className?: string }) => (
  <svg className={className} viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
    {/* Caret 'A' shape */}
    <path d="M256 50 L80 430" stroke="currentColor" strokeWidth="36" strokeLinecap="round" strokeLinejoin="round"/>
    <path d="M256 50 L380 340" stroke="currentColor" strokeWidth="36" strokeLinecap="round" strokeLinejoin="round"/>
    {/* Greek Alpha 'α' loops & tails */}
    <path d="M230 320 C180 320 180 230 230 230 C280 230 280 320 230 320 Z" stroke="currentColor" strokeWidth="36" strokeLinecap="round" strokeLinejoin="round"/>
    <path d="M200 190 C280 240 330 280 430 420" stroke="currentColor" strokeWidth="36" strokeLinecap="round" strokeLinejoin="round"/>
    <path d="M200 425 C280 340 330 245 425 210" stroke="currentColor" strokeWidth="36" strokeLinecap="round" strokeLinejoin="round"/>
  </svg>
);

const RoboAvatar = () => (
  <svg viewBox="0 0 400 400" className="w-full h-[360px]" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="200" cy="200" r="160" fill="url(#avatarGlow)" opacity="0.15" />
    
    {/* Tracks / Base elements */}
    <rect x="110" y="320" width="180" height="30" rx="8" fill="#1E293B" stroke="#475569" strokeWidth="4" />
    <line x1="140" y1="335" x2="260" y2="335" stroke="#64748B" strokeWidth="6" strokeDasharray="12 8" />

    {/* Body / Main Chassis */}
    <rect x="130" y="200" width="140" height="120" rx="16" fill="#334155" stroke="#475569" strokeWidth="6" />
    <rect x="150" y="220" width="100" height="40" rx="6" fill="#1E293B" />
    <line x1="160" y1="230" x2="240" y2="230" stroke="#6366F1" strokeWidth="4" />
    <line x1="160" y1="240" x2="210" y2="240" stroke="#818CF8" strokeWidth="4" />
    <circle cx="230" cy="245" r="4" fill="#10B981" />

    {/* Neck Joint */}
    <rect x="185" y="170" width="30" height="30" rx="4" fill="#475569" stroke="#64748B" strokeWidth="4" />
    <path d="M175 185 L225 185" stroke="#1E293B" strokeWidth="4" />

    {/* Head / Binocular Eyes (Wall-E style) */}
    <g transform="translate(115, 95)">
      <rect x="0" y="10" width="80" height="65" rx="20" transform="rotate(-5 40 42.5)" fill="#334155" stroke="#475569" strokeWidth="6" />
      <circle cx="40" cy="42" r="24" fill="#0F172A" stroke="#64748B" strokeWidth="4" />
      <circle cx="40" cy="42" r="16" fill="#1E293B" />
      <circle cx="34" cy="36" r="6" fill="#6366F1" opacity="0.8" />
      <circle cx="44" cy="46" r="3" fill="#FFFFFF" />
    </g>
    
    <g transform="translate(205, 95)">
      <rect x="0" y="10" width="80" height="65" rx="20" transform="rotate(5 40 42.5)" fill="#334155" stroke="#475569" strokeWidth="6" />
      <circle cx="40" cy="42" r="24" fill="#0F172A" stroke="#64748B" strokeWidth="4" />
      <circle cx="40" cy="42" r="16" fill="#1E293B" />
      <circle cx="34" cy="36" r="6" fill="#6366F1" opacity="0.8" />
      <circle cx="44" cy="46" r="3" fill="#FFFFFF" />
    </g>

    <rect x="185" y="125" width="30" height="15" rx="4" fill="#1E293B" />

    {/* External wires */}
    <path d="M130 240 Q110 260 120 280" stroke="#F43F5E" strokeWidth="3" fill="none" />
    <path d="M270 240 Q290 260 280 280" stroke="#3B82F6" strokeWidth="3" fill="none" />
    
    <defs>
      <radialGradient id="avatarGlow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#6366F1" />
        <stop offset="100%" stopColor="#4F46E5" />
      </radialGradient>
    </defs>
  </svg>
);

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-[#FBFBFE] text-[#1E293B] relative overflow-y-auto overflow-x-hidden font-sans antialiased selection:bg-indigo-100 selection:text-indigo-900">
      {/* Background radial glow */}
      <div className="absolute top-0 right-0 w-[800px] h-[800px] bg-gradient-to-br from-indigo-100/40 via-purple-50/20 to-transparent rounded-full blur-[140px] pointer-events-none" />
      <div className="absolute bottom-10 left-[-100px] w-[500px] h-[500px] bg-gradient-to-tr from-purple-100/30 to-blue-50/20 rounded-full blur-[120px] pointer-events-none" />

      {/* Top Navbar */}
      <header className="px-6 py-5 flex items-center justify-between max-w-6xl mx-auto w-full relative z-10">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#6366F1] to-[#4F46E5] flex items-center justify-center shadow-md shadow-indigo-500/10">
            <AlphaLogo className="w-4.5 h-4.5 text-white" />
          </div>
          <span className="font-extrabold tracking-tight text-lg text-slate-900">
            Alpha <span className="text-[#6366F1]">Sales Agent</span>
          </span>
        </div>
        
        <nav className="hidden md:flex items-center gap-8 text-sm font-semibold text-slate-500">
          <a href="#features" className="hover:text-slate-900 transition-colors">Features</a>
          <a href="#pricing" className="hover:text-slate-900 transition-colors">Pricing</a>
          <a href="https://www.alpha-devs.cloud" target="_blank" rel="noopener noreferrer" className="hover:text-slate-900 transition-colors flex items-center gap-1">
            Alpha Devs 
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
          </a>
        </nav>

        <div className="flex items-center gap-3">
          <Link href="/login" className="text-slate-500 hover:text-slate-900 font-semibold text-sm px-3 py-2 transition-colors">
            Sign in
          </Link>
          <Link
            href="/register"
            className="rounded-full bg-slate-900 hover:bg-slate-800 px-6 py-2.5 font-bold text-sm text-white transition-all shadow-sm hover:scale-[1.01] flex items-center gap-1.5"
          >
            Get Started
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
            </svg>
          </Link>
        </div>
      </header>

      {/* Hero */}
      <main className="max-w-6xl mx-auto px-6 pt-12 pb-24 relative z-10">
        <div className="grid lg:grid-cols-12 gap-12 items-center">
          <div className="lg:col-span-7 space-y-8">
            <div className="space-y-4">
              <div className="inline-flex items-center gap-1.5 bg-indigo-50/80 border border-indigo-100 rounded-full px-3.5 py-1 text-xs text-[#4F46E5] font-bold">
                <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                Trial: 7 Days & 30 Free Minutes Included
              </div>
              
              <h1 className="text-4xl sm:text-5xl font-black text-slate-950 leading-[1.1] tracking-tight">
                Autonomous B2B <br />
                <span className="text-transparent bg-clip-text bg-gradient-to-r from-[#4F46E5] to-[#818CF8]">
                  Sales SDR Agent
                </span>
              </h1>
              
              <p className="text-sm sm:text-base text-slate-500 max-w-lg leading-relaxed pt-1">
                Qualify leads, schedule appointments, and query inventory databases automatically using an LLM-powered voice calling agent built on multi-tenant architecture.
              </p>
            </div>

            {/* Trial command entry mock */}
            <div className="relative max-w-md shadow-sm border border-slate-200/80 rounded-full">
              <input 
                type="text"
                disabled
                placeholder="Connect your database or start trial..."
                className="w-full rounded-full bg-white/80 backdrop-blur-sm pl-6 pr-24 py-3.5 text-xs text-slate-700 placeholder-slate-400 focus:outline-none"
              />
              <Link 
                href="/register" 
                className="absolute right-1.5 top-1.5 bottom-1.5 rounded-full bg-[#4F46E5] hover:bg-[#4338CA] text-white font-bold text-xs px-5 flex items-center gap-1 transition-all shadow-sm"
              >
                Go
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
              </Link>
            </div>

            {/* Horizontal pills */}
            <div className="flex flex-wrap gap-2 pt-2">
              {["Outbound Dialing", "SQL Schema Scanning", "WebRTC Client Widget"].map((pill) => (
                <span 
                  key={pill} 
                  className="rounded-full bg-slate-100 px-4 py-2 font-bold text-[10px] uppercase tracking-wider text-slate-500 border border-slate-200/50"
                >
                  {pill}
                </span>
              ))}
            </div>
          </div>

          {/* Tech-heavy Wall-E style SVG Robot Avatar */}
          <div className="lg:col-span-5 flex justify-center lg:justify-end">
            <div className="relative rounded-3xl overflow-hidden bg-white border border-slate-200/80 p-3 shadow-md max-w-sm w-full flex items-center justify-center">
              <RoboAvatar />
            </div>
          </div>
        </div>

        {/* Feature Sections */}
        <section className="mt-32 space-y-12" id="features">
          <div className="text-center space-y-3">
            <h2 className="text-2xl font-black text-slate-900 tracking-tight">Enterprise Infrastructure Capabilities</h2>
            <p className="text-slate-500 text-sm max-w-md mx-auto">
              Automated pipelines built to manage database contexts, voice streams, and client handoffs.
            </p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-6">
            {[
              { 
                title: "Voice WebRTC Widget", 
                desc: "Embed an audio call button directly on customer-facing portals.",
                icon: (
                  <svg className="w-5 h-5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                )
              },
              { 
                title: "SQL Schema Scanners", 
                desc: "Scan and map POS or custom relational tables dynamically.",
                icon: (
                  <svg className="w-5 h-5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
                  </svg>
                )
              },
              { 
                title: "Multi-Source Adapters", 
                desc: "Query PostgreSQL, Shopify, and MySQL simultaneously in the call loop.",
                icon: (
                  <svg className="w-5 h-5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                  </svg>
                )
              },
              { 
                title: "Supervisor Handoffs", 
                desc: "Automatic alerts sent to supervisors via WhatsApp on intent qualification.",
                icon: (
                  <svg className="w-5 h-5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
                  </svg>
                )
              }
            ].map((feat) => (
              <div key={feat.title} className="rounded-2xl border border-slate-200/70 bg-white p-6 shadow-sm flex flex-col justify-between hover:shadow-md transition-shadow">
                <div className="space-y-4">
                  <div className="w-10 h-10 rounded-xl bg-indigo-50 flex items-center justify-center">
                    {feat.icon}
                  </div>
                  <h3 className="font-extrabold text-slate-800 text-sm">{feat.title}</h3>
                  <p className="text-xs text-slate-500 leading-relaxed">{feat.desc}</p>
                </div>
                <Link href="/register" className="text-indigo-600 hover:text-indigo-700 font-bold text-xs mt-6 inline-flex items-center gap-1 transition-colors">
                  Get Started
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                  </svg>
                </Link>
              </div>
            ))}
          </div>
        </section>

        {/* Pricing Tiers */}
        <section className="mt-32 space-y-12" id="pricing">
          <div className="text-center space-y-3">
            <h2 className="text-2xl font-black text-slate-900 tracking-tight">SDR Agent Subscription Plans</h2>
            <p className="text-slate-500 text-sm max-w-md mx-auto">
              Select a plan that matches your monthly call volumes. Upgrade or cancel anytime.
            </p>
          </div>

          <div className="grid md:grid-cols-4 gap-6 max-w-5xl mx-auto">
            {/* Free Trial */}
            <div className="rounded-2xl border border-indigo-200 bg-indigo-50/30 p-6 flex flex-col justify-between relative shadow-sm">
              <div className="absolute top-3.5 right-3.5 rounded-full bg-indigo-600 text-white text-[8px] font-black uppercase px-2.5 py-0.5 tracking-wider">
                Trial
              </div>
              <div className="space-y-4">
                <h3 className="font-extrabold text-indigo-800 text-xs">Free Trial</h3>
                <div className="text-2xl font-black text-slate-950">
                  $0 <span className="text-xs font-normal text-slate-400">/ 7 Days</span>
                </div>
                <p className="text-xs text-slate-500 leading-relaxed">
                  Ideal for testing voice quality, scan adapters, and custom system prompts.
                </p>
                <ul className="text-xs space-y-2 text-slate-600 pt-2 border-t border-indigo-100">
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    <strong>30 voice minutes</strong>
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    SQL Mapped context
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    gemini-2.5-flash-lite
                  </li>
                </ul>
              </div>
              <Link href="/register" className="w-full mt-6 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-center font-bold text-xs py-2.5 transition-all shadow-sm shadow-indigo-600/10">
                Start Trial
              </Link>
            </div>

            {/* Starter Plan */}
            <div className="rounded-2xl border border-slate-200 bg-white p-6 flex flex-col justify-between shadow-sm">
              <div className="space-y-4">
                <h3 className="font-extrabold text-slate-700 text-xs">SaaS Starter</h3>
                <div className="text-2xl font-black text-slate-950">
                  $49 <span className="text-xs font-normal text-slate-400">/ month</span>
                </div>
                <p className="text-xs text-slate-500 leading-relaxed">
                  For small business teams beginning outbound sales pipelines.
                </p>
                <ul className="text-xs space-y-2 text-slate-600 pt-2 border-t border-slate-100">
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    <strong>300 voice minutes</strong>
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    SQL Adapter scan
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    Overage: $0.19 / min
                  </li>
                </ul>
              </div>
              <Link href="/register?plan=starter" className="w-full mt-6 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-center font-bold text-xs py-2.5 transition-all">
                Select Starter
              </Link>
            </div>

            {/* Professional Plan */}
            <div className="rounded-2xl border-2 border-[#6366F1] bg-white p-6 flex flex-col justify-between shadow-md relative">
              <div className="absolute top-[-11px] left-1/2 -translate-x-1/2 rounded-full bg-[#6366F1] text-white text-[8px] font-black uppercase px-3 py-1 shadow-sm tracking-wider">
                Popular
              </div>
              <div className="space-y-4">
                <h3 className="font-extrabold text-[#6366F1] text-xs">Professional</h3>
                <div className="text-2xl font-black text-slate-950">
                  $199 <span className="text-xs font-normal text-slate-400">/ month</span>
                </div>
                <p className="text-xs text-slate-500 leading-relaxed">
                  For active shops requiring high-speed database context integration.
                </p>
                <ul className="text-xs space-y-2 text-slate-600 pt-2 border-t border-slate-100">
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-[#6366F1]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    <strong>1,500 voice minutes</strong>
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-[#6366F1]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    Multi-table DB mapping
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-[#6366F1]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    Overage: $0.15 / min
                  </li>
                </ul>
              </div>
              <Link href="/register?plan=professional" className="w-full mt-6 rounded-xl bg-[#6366F1] hover:bg-[#4F46E5] text-white text-center font-bold text-xs py-2.5 transition-all shadow-md shadow-indigo-600/10 hover:scale-[1.01]">
                Select Professional
              </Link>
            </div>

            {/* Enterprise Plan */}
            <div className="rounded-2xl border border-slate-200 bg-white p-6 flex flex-col justify-between shadow-sm">
              <div className="space-y-4">
                <h3 className="font-extrabold text-slate-700 text-xs">Enterprise</h3>
                <div className="text-2xl font-black text-slate-950">
                  $499 <span className="text-xs font-normal text-slate-400">/ month</span>
                </div>
                <p className="text-xs text-slate-500 leading-relaxed">
                  For corporations with custom database infrastructures.
                </p>
                <ul className="text-xs space-y-2 text-slate-600 pt-2 border-t border-slate-100">
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    <strong>4,500 voice minutes</strong>
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    Custom rate-limiting settings
                  </li>
                  <li className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    Overage: $0.12 / min
                  </li>
                </ul>
              </div>
              <Link href="/register?plan=enterprise" className="w-full mt-6 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-center font-bold text-xs py-2.5 transition-all">
                Select Enterprise
              </Link>
            </div>
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-200 bg-white py-12 relative z-10">
        <div className="max-w-6xl mx-auto px-6 flex flex-col md:flex-row items-center justify-between gap-6">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded bg-[#6366F1] flex items-center justify-center">
              <AlphaLogo className="w-4 h-4 text-white" />
            </div>
            <span className="font-extrabold tracking-tight text-sm text-[#0A0E1A]">
              Alpha Sales Agent
            </span>
          </div>

          <div className="text-center md:text-right text-xs text-slate-500 space-y-1.5">
            <div>
              A product of <span className="font-bold text-slate-800">Alpha Devs</span>.
            </div>
            <div>
              Visit us and explore more of our services and agents at{" "}
              <a 
                href="https://www.alpha-devs.cloud" 
                target="_blank" 
                rel="noopener noreferrer" 
                className="text-indigo-600 hover:underline font-semibold"
              >
                www.alpha-devs.cloud
              </a>.
            </div>
          </div>
        </div>
        <div className="text-center text-slate-400 text-[10px] mt-8 pt-4 border-t border-slate-50">
          &copy; 2026 Alpha Sales Agent. All rights reserved.
        </div>
      </footer>
    </div>
  );
}
