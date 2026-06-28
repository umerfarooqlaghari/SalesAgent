"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  authHeaders,
  fetchMe,
  getAccessToken,
  getBackendUrl,
  getStoredUser,
  clearSession,
  type AuthUser,
} from "@/lib/auth";

type PlatformStats = {
  tenant_count: number;
  user_count: number;
  lead_count: number;
  order_count: number;
  appointment_count: number;
  conversation_count: number;
};

type TenantRow = {
  tenant_id: string;
  org_name: string;
  status: string;
  owner_email?: string;
  created_at?: string;
  lead_count: number;
  order_count: number;
  appointment_count: number;
  conversation_count: number;
};

type UserRow = {
  user_id: string;
  email: string;
  name: string;
  role: string;
  tenant_id?: string | null;
  status: string;
  created_at?: string;
};

export default function SuperAdminPage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [tenants, setTenants] = useState<TenantRow[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [tab, setTab] = useState<"overview" | "tenants" | "users">("overview");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const init = async () => {
      const token = getAccessToken();
      if (!token) {
        router.replace("/login");
        return;
      }
      const me = await fetchMe(getBackendUrl());
      if (!me || me.role !== "super_admin") {
        router.replace(me ? "/dashboard" : "/login");
        return;
      }
      setUser(me);
      try {
        const headers = authHeaders();
        const base = getBackendUrl();
        const [statsRes, tenantsRes, usersRes] = await Promise.all([
          fetch(`${base}/api/superadmin/stats`, { headers }),
          fetch(`${base}/api/superadmin/tenants`, { headers }),
          fetch(`${base}/api/superadmin/users`, { headers }),
        ]);
        if (statsRes.ok) setStats(await statsRes.json());
        if (tenantsRes.ok) {
          const data = await tenantsRes.json();
          setTenants(data.tenants || []);
        }
        if (usersRes.ok) {
          const data = await usersRes.json();
          setUsers(data.users || []);
        }
      } catch {
        setError("Failed to load platform data");
      } finally {
        setLoading(false);
      }
    };
    init();
  }, [router]);

  const handleLogout = () => {
    clearSession();
    router.push("/");
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-[#0A0E1A] flex items-center justify-center text-slate-400">
        Loading platform…
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#0A0E1A] text-white">
      <header className="border-b border-[#1F293D] px-6 py-4 flex items-center justify-between">
        <div>
          <p className="text-xs text-amber-400 font-bold uppercase tracking-widest">Super Admin</p>
          <h1 className="text-xl font-bold">Platform Console</h1>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-400 hidden sm:inline">{user?.email || getStoredUser()?.email}</span>
          <button
            onClick={handleLogout}
            className="rounded-lg border border-slate-600 px-3 py-1.5 text-slate-300 hover:bg-slate-800"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="border-b border-[#1F293D] px-6 flex gap-1">
        {(["overview", "tenants", "users"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-3 text-sm font-semibold capitalize border-b-2 transition-colors ${
              tab === t ? "border-sky-500 text-sky-400" : "border-transparent text-slate-400 hover:text-white"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      <main className="max-w-6xl mx-auto px-6 py-8">
        {error && <p className="text-rose-400 mb-4 text-sm">{error}</p>}

        {tab === "overview" && stats && (
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {[
              { label: "Tenants", value: stats.tenant_count },
              { label: "Users", value: stats.user_count },
              { label: "Leads", value: stats.lead_count },
              { label: "Orders", value: stats.order_count },
              { label: "Appointments", value: stats.appointment_count },
              { label: "Conversations", value: stats.conversation_count },
            ].map((s) => (
              <div key={s.label} className="rounded-xl border border-[#1F293D] bg-[#111726] p-6">
                <p className="text-xs text-slate-400 uppercase tracking-wider">{s.label}</p>
                <p className="text-3xl font-bold mt-2">{s.value.toLocaleString()}</p>
              </div>
            ))}
          </div>
        )}

        {tab === "tenants" && (
          <div className="overflow-x-auto rounded-xl border border-[#1F293D]">
            <table className="w-full text-sm">
              <thead className="bg-[#111726] text-left text-xs text-slate-400 uppercase">
                <tr>
                  <th className="px-4 py-3">Organization</th>
                  <th className="px-4 py-3">Tenant ID</th>
                  <th className="px-4 py-3">Owner</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Leads</th>
                  <th className="px-4 py-3">Orders</th>
                </tr>
              </thead>
              <tbody>
                {tenants.map((t) => (
                  <tr key={t.tenant_id} className="border-t border-[#1F293D] hover:bg-[#111726]/50">
                    <td className="px-4 py-3 font-medium">{t.org_name}</td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">{t.tenant_id}</td>
                    <td className="px-4 py-3 text-slate-300">{t.owner_email || "—"}</td>
                    <td className="px-4 py-3">
                      <span
                        className={`text-xs font-bold uppercase ${
                          t.status === "active" ? "text-emerald-400" : "text-amber-400"
                        }`}
                      >
                        {t.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">{t.lead_count}</td>
                    <td className="px-4 py-3">{t.order_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === "users" && (
          <div className="overflow-x-auto rounded-xl border border-[#1F293D]">
            <table className="w-full text-sm">
              <thead className="bg-[#111726] text-left text-xs text-slate-400 uppercase">
                <tr>
                  <th className="px-4 py-3">Email</th>
                  <th className="px-4 py-3">Name</th>
                  <th className="px-4 py-3">Role</th>
                  <th className="px-4 py-3">Tenant</th>
                  <th className="px-4 py-3">Status</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.user_id} className="border-t border-[#1F293D] hover:bg-[#111726]/50">
                    <td className="px-4 py-3">{u.email}</td>
                    <td className="px-4 py-3">{u.name}</td>
                    <td className="px-4 py-3">
                      <span
                        className={`text-xs font-bold uppercase ${
                          u.role === "super_admin" ? "text-amber-400" : "text-sky-400"
                        }`}
                      >
                        {u.role}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">{u.tenant_id || "—"}</td>
                    <td className="px-4 py-3">{u.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="mt-8 text-center text-xs text-slate-500">
          <Link href="/" className="text-sky-400 hover:underline">
            Back to home
          </Link>
        </p>
      </main>
    </div>
  );
}
