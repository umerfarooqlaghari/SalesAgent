"use client";

import React, { useState, useEffect } from "react";
import { getAccessToken, getBackendUrl } from "@/lib/auth";

interface AdminBillingProps {
  backendUrl: string;
}

interface Plan {
  id: string;
  name: string;
  price: number;
  minutes: number;
}

export default function AdminBilling({ backendUrl }: AdminBillingProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [billingInfo, setBillingInfo] = useState<any>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [upgradingPlanId, setUpgradingPlanId] = useState<string | null>(null);

  const fetchBillingData = async () => {
    setLoading(true);
    setError(null);
    try {
      const headers = {
        Authorization: `Bearer ${getAccessToken()}`,
      };

      // Fetch config & plans
      const configRes = await fetch(`${backendUrl}/api/billing/config`, { headers });
      if (configRes.ok) {
        const configData = await configRes.json();
        setPlans(configData.plans || []);
      }

      // Fetch current tenant metadata (tier, used_minutes, allowed_minutes)
      const tenantRes = await fetch(`${backendUrl}/api/admin/tenant`, { headers });
      if (tenantRes.ok) {
        const tenantData = await tenantRes.json();
        setBillingInfo(tenantData);
      } else {
        setError("Failed to retrieve tenant billing information.");
      }
    } catch (err: any) {
      console.error(err);
      setError("Network error fetching billing details.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBillingData();
  }, [backendUrl]);

  const handleCheckout = async (priceId: string) => {
    setUpgradingPlanId(priceId);
    setError(null);
    try {
      const res = await fetch(`${backendUrl}/api/billing/checkout`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${getAccessToken()}`,
        },
        body: JSON.stringify({ price_id: priceId }),
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "Checkout creation failed");
      }

      const data = await res.json();
      if (data.checkout_url) {
        // Redirect to checkout session
        window.location.href = data.checkout_url;
      }
    } catch (err: any) {
      console.error(err);
      setError(err.message || "Failed to start payment checkout.");
    } finally {
      setUpgradingPlanId(null);
    }
  };

  const handlePortalRedirect = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${backendUrl}/api/billing/portal`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${getAccessToken()}`,
        },
      });

      if (!res.ok) {
        throw new Error("Failed to load customer billing portal");
      }

      const data = await res.json();
      if (data.portal_url) {
        window.location.href = data.portal_url;
      }
    } catch (err: any) {
      console.error(err);
      setError(err.message || "Portal redirect failed.");
    } finally {
      setLoading(false);
    }
  };

  if (loading && !billingInfo) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-[#A084E8] border-t-transparent" />
      </div>
    );
  }

  const used = billingInfo?.used_minutes || 0;
  const allowed = billingInfo?.allowed_minutes || 30;
  const usagePercentage = Math.min(100, Math.round((used / allowed) * 100));
  const currentTier = billingInfo?.tier || "free";

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 text-slate-800 space-y-10">
      {/* Page Title */}
      <div className="space-y-1">
        <h1 className="text-2xl font-extrabold tracking-tight text-slate-900">Billing & Trials</h1>
        <p className="text-sm text-slate-500">Manage your subscription, review minute usage, or upgrade your agent service package.</p>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-xs font-semibold text-red-800">
          ❌ {error}
        </div>
      )}

      {/* Usage Monitor Widget */}
      <div className="grid md:grid-cols-3 gap-6">
        <div className="md:col-span-2 rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm space-y-6">
          <div className="flex justify-between items-center">
            <div>
              <h2 className="text-base font-extrabold text-slate-950">Call Usage Monitor</h2>
              <p className="text-xs text-slate-400">Total duration of outbound and widget voice calls.</p>
            </div>
            <span className="rounded-full bg-purple-100 text-purple-700 text-xs font-black uppercase px-3 py-1">
              Active Tier: {currentTier}
            </span>
          </div>

          <div className="space-y-2">
            <div className="flex justify-between text-xs font-bold text-slate-600">
              <span>{used.toFixed(1)} mins used</span>
              <span>{allowed} mins limit</span>
            </div>
            <div className="h-3 w-full bg-slate-100 rounded-full overflow-hidden">
              <div 
                className="h-full bg-gradient-to-r from-purple-500 to-[#A084E8] rounded-full transition-all duration-500" 
                style={{ width: `${usagePercentage}%` }}
              />
            </div>
            <div className="text-[10px] text-slate-400">
              Usage updates automatically after each call is finalized via webhook logs.
            </div>
          </div>
        </div>

        {/* Action Panel */}
        <div className="rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm flex flex-col justify-between">
          <div className="space-y-2">
            <h3 className="text-sm font-extrabold text-slate-950">Subscription Portal</h3>
            <p className="text-xs text-slate-400 leading-relaxed">
              If you have an active Stripe subscription, you can manage billing invoices, update payment methods, or cancel anytime.
            </p>
          </div>
          {currentTier !== "free" && currentTier !== "trial" ? (
            <button
              type="button"
              onClick={handlePortalRedirect}
              className="w-full mt-4 rounded-xl border border-slate-300 hover:bg-slate-50 text-slate-700 font-bold text-xs py-2.5 transition-all text-center"
            >
              Manage Subscription in Stripe
            </button>
          ) : (
            <div className="text-xs text-purple-600 font-bold mt-4">
              ✨ Subscribe to a premium tier below to unlock custom portal configs.
            </div>
          )}
        </div>
      </div>

      {/* Upgrade Packages */}
      <div className="space-y-6">
        <h2 className="text-base font-extrabold text-slate-950">Available Subscription Upgrades</h2>
        <div className="grid md:grid-cols-3 gap-6">
          {plans.map((plan) => {
            const isCurrent = currentTier.toLowerCase() === plan.name.toLowerCase();
            const upgrading = upgradingPlanId === plan.id;
            return (
              <div 
                key={plan.id} 
                className={`rounded-2xl border p-6 flex flex-col justify-between bg-white transition-all shadow-sm ${
                  isCurrent 
                    ? "border-2 border-[#A084E8] ring-2 ring-purple-100" 
                    : "border-slate-200/80 hover:shadow-md"
                }`}
              >
                <div className="space-y-4">
                  <div className="flex justify-between items-center">
                    <h3 className="font-extrabold text-slate-900 text-sm">{plan.name}</h3>
                    {isCurrent && (
                      <span className="rounded-full bg-purple-50 text-[#A084E8] text-[9px] font-black uppercase px-2.5 py-0.5">
                        Current
                      </span>
                    )}
                  </div>
                  <div className="text-2xl font-black text-slate-950">
                    ${plan.price}
                    <span className="text-xs font-normal text-slate-400"> / month</span>
                  </div>
                  <p className="text-xs text-slate-500">
                    Includes {plan.minutes} conversation call minutes per month with low-latency tool execution.
                  </p>
                  <ul className="text-xs space-y-2 text-slate-600 pt-2 border-t border-slate-100">
                    <li>📞 <strong>{plan.minutes} voice minutes</strong></li>
                    <li>🤖 gemini-2.5-flash-lite</li>
                    <li>⚡ Warm SQL pool cache reuse</li>
                    <li>✅ WhatsApp handoff supervisor</li>
                  </ul>
                </div>

                <button
                  type="button"
                  disabled={isCurrent || upgrading}
                  onClick={() => handleCheckout(plan.id)}
                  className={`w-full mt-6 rounded-xl font-bold text-xs py-2.5 transition-all text-center ${
                    isCurrent
                      ? "bg-slate-100 text-slate-400 cursor-not-allowed"
                      : upgrading
                        ? "bg-purple-100 text-[#A084E8] cursor-wait"
                        : "bg-[#A084E8] hover:bg-[#8D72E1] text-white shadow-md shadow-purple-500/10 hover:scale-[1.02]"
                  }`}
                >
                  {upgrading ? "Processing…" : isCurrent ? "Active Package" : `Choose ${plan.name}`}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
