"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import SqlSchemaWizard, { SQL_PROVIDERS, ui, type DiscoverResult } from "./SqlSchemaWizard";

type FieldSchema = {
  key: string;
  label: string;
  type: string;
  required?: boolean;
  placeholder?: string;
  help_text?: string;
  default?: unknown;
};

type ProviderSchema = {
  id: string;
  label: string;
  description?: string;
  fields: FieldSchema[];
};

type CategorySchema = {
  id: string;
  label: string;
  description?: string;
  allow_multiple: boolean;
  providers: ProviderSchema[];
};

type InventorySource = {
  id: string;
  enabled: boolean;
  provider: string;
  priority: number;
  label?: string;
  config: Record<string, unknown>;
};

type IntegrationBlock = {
  enabled: boolean;
  provider: string;
  config: Record<string, unknown>;
};

type IntegrationsState = {
  inventory: { enabled: boolean; sources: InventorySource[] };
  crm: IntegrationBlock;
  calendar: IntegrationBlock;
};

interface Props {
  backendUrl: string;
  getHeaders: () => Record<string, string>;
}

// F18: this took a non-optional CategorySchema, so both call sites reached for
// `invCategory!`. If /api/admin/integration-schemas fails while /api/admin/tenant
// succeeds, invCategory is undefined and clicking "Add inventory source" threw
// inside here and blanked the whole tab. An empty default is the correct answer.
function defaultConfigForProvider(
  category: CategorySchema | undefined,
  providerId: string
): Record<string, unknown> {
  const provider = category?.providers.find((p) => p.id === providerId);
  const config: Record<string, unknown> = {};
  provider?.fields.forEach((f) => {
    if (f.default !== undefined && f.default !== null) config[f.key] = f.default;
  });
  return config;
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="inline-flex items-center gap-2 cursor-pointer select-none">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
      />
      <span className="text-sm font-medium text-gray-700">{label}</span>
    </label>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: FieldSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (field.type === "boolean") {
    return <Toggle checked={Boolean(value)} onChange={onChange} label={field.label} />;
  }

  if (field.type === "json" || field.type === "textarea") {
    const str = typeof value === "string" ? value : value ? JSON.stringify(value, null, 2) : "";
    return (
      <textarea
        rows={field.type === "json" ? 5 : 3}
        className={ui.input}
        placeholder={field.placeholder}
        value={str}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <input
      type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
      className={ui.input}
      placeholder={field.placeholder}
      value={value === undefined || value === null ? "" : String(value)}
      onChange={(e) =>
        onChange(field.type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value)
      }
    />
  );
}

function SectionCard({
  title,
  description,
  enabled,
  onEnabledChange,
  children,
}: {
  title: string;
  description?: string;
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  children: React.ReactNode;
}) {
  return (
    <section className={`${ui.card} overflow-hidden`}>
      <div className={`${ui.cardHeader} flex flex-wrap items-center justify-between gap-3 bg-white`}>
        <div>
          <h3 className="text-base font-semibold text-gray-900">{title}</h3>
          {description && <p className="text-sm text-gray-500 mt-0.5">{description}</p>}
        </div>
        <Toggle checked={enabled} onChange={onEnabledChange} label="Enabled" />
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

function ConnectionFields({
  categoryId,
  category,
  providerId,
  config,
  onConfigChange,
  backendUrl,
  getHeaders,
  sourceId,
  getDiscovery,
  setDiscovery,
  setMessage,
}: {
  categoryId: string;
  category: CategorySchema | undefined;
  providerId: string;
  config: Record<string, unknown>;
  onConfigChange: (cfg: Record<string, unknown>) => void;
  backendUrl: string;
  getHeaders: () => Record<string, string>;
  sourceId?: string;
  getDiscovery: (key: string) => DiscoverResult | null;
  setDiscovery: (key: string, data: DiscoverResult | null) => void;
  setMessage: (msg: string, ok: boolean | null) => void;
}) {
  const provider = category?.providers.find((p) => p.id === providerId);
  if (!provider) return null;
  const isSql = SQL_PROVIDERS.has(providerId);
  const fields = isSql ? provider.fields.filter((f) => f.key !== "table_map") : provider.fields;

  return (
    <div className="space-y-4">
      {provider.description && <p className="text-sm text-gray-500">{provider.description}</p>}

      {isSql && (
        <div className={`${ui.card} p-5`}>
          <h4 className="text-sm font-semibold text-gray-900 mb-4">Connection details</h4>
          <div className="grid sm:grid-cols-2 gap-4">
            {fields.map((field, idx) => (
              <div key={`${field.key}-${idx}`} className={field.type === "boolean" ? "sm:col-span-2" : ""}>
                {field.type !== "boolean" && (
                  <label className={ui.label}>
                    {field.label}
                    {field.required && <span className="text-red-500 ml-0.5">*</span>}
                  </label>
                )}
                <FieldInput
                  field={field}
                  value={config[field.key]}
                  onChange={(v) => onConfigChange({ ...config, [field.key]: v })}
                />
                {field.help_text && <p className={ui.hint}>{field.help_text}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {!isSql &&
        fields.map((field, idx) => (
          <div key={`${field.key}-${idx}`}>
            {field.type !== "boolean" && (
              <label className={ui.label}>
                {field.label}
                {field.required && <span className="text-red-500 ml-0.5">*</span>}
              </label>
            )}
            <FieldInput
              field={field}
              value={config[field.key]}
              onChange={(v) => onConfigChange({ ...config, [field.key]: v })}
            />
            {field.help_text && <p className={ui.hint}>{field.help_text}</p>}
          </div>
        ))}

      {isSql && (
        <SqlSchemaWizard
          category={categoryId}
          provider={providerId}
          config={config}
          onConfigChange={onConfigChange}
          backendUrl={backendUrl}
          getHeaders={getHeaders}
          sourceId={sourceId}
          discoveryKey={`${categoryId}-${sourceId ?? providerId}`}
          discovered={getDiscovery(`${categoryId}-${sourceId ?? providerId}`)}
          onDiscovered={(data) => setDiscovery(`${categoryId}-${sourceId ?? providerId}`, data)}
          onMessage={(msg) => setMessage(msg, msg.toLowerCase().includes("found") || msg.toLowerCase().includes("success") ? true : msg ? null : null)}
        />
      )}
    </div>
  );
}

function AdminIntegrations({ backendUrl, getHeaders }: Props) {
  const [schemas, setSchemas] = useState<CategorySchema[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationsState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState("");
  const [orgName, setOrgName] = useState("");
  const [companyDescription, setCompanyDescription] = useState("");
  const [initialCompanyDescription, setInitialCompanyDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [initialSystemPrompt, setInitialSystemPrompt] = useState("");
  const [resettingPrompt, setResettingPrompt] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [status, setStatus] = useState("");
  const [statusOk, setStatusOk] = useState<boolean | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [syncingKnowledge, setSyncingKnowledge] = useState(false);
  const [discoveryCache, setDiscoveryCache] = useState<Record<string, DiscoverResult>>({});

  // F17 + F22: `getDiscovery` used to read sessionStorage during render. That
  // is an impure render (hydration mismatch) and it handed a brand-new object
  // identity to the wizard on every single render, so nothing downstream could
  // ever be memoised. It also meant a full map of the customer's production
  // schema sat on disk. Discovery is now in-memory state only, and any copy an
  // older build left behind is swept on mount.
  useEffect(() => {
    try {
      const stale: string[] = [];
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        if (k && k.startsWith("alpha-discovery-")) stale.push(k);
      }
      stale.forEach((k) => sessionStorage.removeItem(k));
    } catch {
      /* ignore */
    }
  }, []);

  const setDiscovery = useCallback((key: string, data: DiscoverResult | null) => {
    setDiscoveryCache((prev) => {
      const next = { ...prev };
      if (data) next[key] = data;
      else delete next[key];
      return next;
    });
  }, []);

  const getDiscovery = useCallback(
    (key: string): DiscoverResult | null => discoveryCache[key] ?? null,
    [discoveryCache]
  );

  const setMessage = (msg: string, ok: boolean | null = null) => {
    setStatus(msg);
    setStatusOk(ok ?? (msg.toLowerCase().includes("ok") || msg.toLowerCase().includes("success") || msg.toLowerCase().includes("found") ? true : msg ? false : null));
  };

  // F08: overlapping loads used to race — Reload while a slow first load was
  // still in flight meant whichever resolved last won, and a resolution after
  // unmount wrote to a dead tree. Every load now carries a generation number
  // and an AbortController; only the newest is allowed to commit.
  const loadSeq = useRef(0);
  const inFlight = useRef<AbortController | null>(null);

  // F01, belt and braces: the parent now passes a stable `getHeaders`, but this
  // panel should not lose a half-typed form the next time somebody drops an
  // inline callback into the JSX. Reading it through a ref keeps `load`'s
  // identity tied to `backendUrl` alone, so a churning parent cannot re-trigger
  // the load effect no matter how it passes its props.
  const getHeadersRef = useRef(getHeaders);
  useEffect(() => {
    getHeadersRef.current = getHeaders;
  }, [getHeaders]);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    setLoading(true);
    setLoadError(null);
    try {
      const headers = getHeadersRef.current();
      const [schemaRes, tenantRes] = await Promise.all([
        fetch(`${backendUrl}/api/admin/integration-schemas`, {
          headers,
          signal: controller.signal,
        }),
        fetch(`${backendUrl}/api/admin/tenant`, {
          headers,
          signal: controller.signal,
        }),
      ]);
      if (seq !== loadSeq.current) return;

      if (schemaRes.ok) {
        const data = await schemaRes.json();
        if (seq !== loadSeq.current) return;
        setSchemas(data.categories || []);
      } else {
        setSchemas([]);
      }

      if (tenantRes.ok) {
        const data = await tenantRes.json();
        if (seq !== loadSeq.current) return;
        setTenantId(data.tenant_id || "");
        setOrgName(data.org_name || "");
        setIntegrations(data.integrations);
        const desc = data.settings?.company_description || "";
        const prompt = data.settings?.system_prompt || "";
        setCompanyDescription(desc);
        setInitialCompanyDescription(desc);
        setSystemPrompt(prompt);
        setInitialSystemPrompt(prompt);
        setLoadError(null);
      } else {
        setLoadError(
          tenantRes.status === 401 || tenantRes.status === 403
            ? "Your session is not allowed to read these settings (HTTP " + tenantRes.status + ")."
            : `Could not load your integration settings (HTTP ${tenantRes.status}).`
        );
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      if (seq !== loadSeq.current) return;
      console.error(e);
      setLoadError(
        e instanceof Error
          ? `Could not reach the backend: ${e.message}`
          : "Could not reach the backend."
      );
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, [backendUrl]);

  useEffect(() => {
    load();
    return () => {
      loadSeq.current++;
      inFlight.current?.abort();
    };
  }, [load]);

  const save = async () => {
    if (!integrations) return;
    setSaving(true);
    setMessage("");
    try {
      const res = await fetch(`${backendUrl}/api/admin/integrations`, {
        method: "PUT",
        headers: getHeaders(),
        body: JSON.stringify({ integrations }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      setIntegrations(data.integrations);

      // F05: this endpoint takes only { integrations }, yet it used to push the
      // server's system_prompt into the textarea while leaving
      // initialSystemPrompt stale. Saving an integration therefore discarded a
      // prompt you were part-way through writing AND left the dirty check
      // permanently wrong. Only accept the server's copy when there is nothing
      // unsaved to lose, and move both values together so "Update settings"
      // stays honest.
      const serverPrompt = data.settings?.system_prompt;
      if (serverPrompt && systemPrompt === initialSystemPrompt) {
        setSystemPrompt(serverPrompt);
        setInitialSystemPrompt(serverPrompt);
      }

      const hub = data.adapter_hub_sync;
      const hubMsg =
        hub?.ok && hub?.synchronized_count
          ? ` Knowledge sync: ${hub.synchronized_count} record(s).`
          : hub?.skipped
            ? " (Adapter-Hub offline — live SQL queries still work.)"
            : "";
      setMessage(`Settings saved successfully.${hubMsg}`, true);
    } catch (e: unknown) {
      setMessage(e instanceof Error ? e.message : "Save failed", false);
    } finally {
      setSaving(false);
    }
  };

  const syncKnowledge = async () => {
    setSyncingKnowledge(true);
    setMessage("");
    try {
      const res = await fetch(`${backendUrl}/api/admin/integrations/sync-knowledge`, {
        method: "POST",
        headers: getHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.message || "Sync failed");
      setMessage(data.message || "Knowledge sync complete.", Boolean(data.ok));
    } catch (e: unknown) {
      setMessage(e instanceof Error ? e.message : "Knowledge sync failed", false);
    } finally {
      setSyncingKnowledge(false);
    }
  };

  const saveAgentSettings = async () => {
    setSavingSettings(true);
    setMessage("");
    try {
      const res = await fetch(`${backendUrl}/api/admin/settings`, {
        method: "PUT",
        headers: getHeaders(),
        body: JSON.stringify({
          settings: {
            company_description: companyDescription,
            system_prompt: systemPrompt,
          },
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      const desc = data.settings?.company_description || "";
      const prompt = data.settings?.system_prompt || "";
      setCompanyDescription(desc);
      setInitialCompanyDescription(desc);
      setSystemPrompt(prompt);
      setInitialSystemPrompt(prompt);
      setMessage("Agent settings saved.", true);
    } catch (e: unknown) {
      setMessage(e instanceof Error ? e.message : "Save failed", false);
    } finally {
      setSavingSettings(false);
    }
  };

  const resetAgentPrompt = async () => {
    setResettingPrompt(true);
    setMessage("");
    try {
      const res = await fetch(`${backendUrl}/api/admin/settings/reset-agent-prompt`, {
        method: "POST",
        headers: getHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Reset failed");
      const prompt = data.settings?.system_prompt || "";
      setSystemPrompt(prompt);
      setInitialSystemPrompt(prompt);
      setMessage("Agent prompt reset for your company.", true);
    } catch (e: unknown) {
      setMessage(e instanceof Error ? e.message : "Reset failed", false);
    } finally {
      setResettingPrompt(false);
    }
  };

  const testSource = async (
    category: string,
    provider: string,
    config: Record<string, unknown>,
    sourceId?: string
  ) => {
    const key = `${category}-${sourceId || provider}`;
    setTesting(key);
    setMessage("");
    try {
      const res = await fetch(`${backendUrl}/api/admin/integrations/test`, {
        method: "POST",
        headers: getHeaders(),
        body: JSON.stringify({ category, provider, config, source_id: sourceId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.message || "Test failed");
      setMessage(`Connection successful.${data.preview ? ` Preview: ${String(data.preview).slice(0, 180)}…` : ""}`, true);
    } catch (e: unknown) {
      setMessage(e instanceof Error ? e.message : "Connection test failed", false);
    } finally {
      setTesting(null);
    }
  };

  // F06: the old early return sat *above* the status banner, so the catch
  // branch's error message could never render — a failed load left a tab
  // permanently reading "Loading integrations…" with no way to retry. Loading
  // and failure are now distinct states, and failure offers a retry.
  if (loading && !integrations) {
    return (
      <div className="flex flex-1 items-center justify-center bg-slate-50 min-h-[60vh]">
        <p className="text-sm text-gray-500">Loading integrations…</p>
      </div>
    );
  }

  if (!integrations) {
    return (
      <div className="flex flex-1 items-center justify-center bg-slate-50 min-h-[60vh] px-4">
        <div className={`${ui.card} max-w-md w-full p-6 text-center space-y-3`}>
          <h3 className="text-base font-semibold text-gray-900">
            Couldn&apos;t load your integrations
          </h3>
          <p className="text-sm text-gray-600">
            {loadError || "The backend did not return your integration settings."}
          </p>
          <p className={ui.hint}>
            Check that the Backend Service URL in the sidebar points at your API, then try again.
          </p>
          <button type="button" onClick={load} disabled={loading} className={ui.btnPrimary}>
            {loading ? "Retrying…" : "Retry"}
          </button>
        </div>
      </div>
    );
  }

  const invCategory = schemas.find((c) => c.id === "inventory");
  const crmCategory = schemas.find((c) => c.id === "crm");
  const calCategory = schemas.find((c) => c.id === "calendar");

  return (
    <div className="flex-1 overflow-y-auto bg-slate-50 min-h-full">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 py-8 space-y-6">
        <header className="border-b border-gray-200 pb-6 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="text-2xl font-semibold text-gray-900 tracking-tight">Integrations</h2>
            <p className="text-sm text-gray-500 mt-1">
              Connect your databases and services. Organization:{" "}
              <code className="rounded bg-gray-100 px-1.5 py-0.5 text-xs font-mono text-gray-700">{tenantId}</code>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button type="button" onClick={save} disabled={saving} className={ui.btnPrimary}>
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </header>

        {status && (
          <div
            className={`rounded-lg border px-4 py-3 text-sm ${
              statusOk === true
                ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                : statusOk === false
                  ? "border-red-200 bg-red-50 text-red-900"
                  : "border-blue-200 bg-blue-50 text-blue-900"
            }`}
          >
            {status}
          </div>
        )}

        {schemas.length === 0 && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 flex flex-wrap items-center justify-between gap-3">
            <span>
              Provider definitions didn&apos;t load, so the connection forms are unavailable. Your
              saved settings are intact.
            </span>
            <button type="button" onClick={load} disabled={loading} className={ui.btnSecondary}>
              {loading ? "Retrying…" : "Retry"}
            </button>
          </div>
        )}

        <section className={`${ui.card} overflow-hidden`}>
          <div className={`${ui.cardHeader} bg-white`}>
            <h3 className="text-base font-semibold text-gray-900">Agent persona</h3>
            <p className="text-sm text-gray-500 mt-0.5">
              The voice and chat agent uses these settings for {orgName || "your organization"}. If it still talks about
              Alpha or SaaS packages, reset the prompt below.
            </p>
          </div>
          <div className="p-5 space-y-4">
            <div>
              <label className={ui.label}>What does your company do? (optional)</label>
              <textarea
                rows={3}
                className={ui.input}
                placeholder="e.g. We build production sets and scenic construction for film and events."
                value={companyDescription}
                onChange={(e) => setCompanyDescription(e.target.value)}
              />
            </div>
            <div>
              <label className={ui.label}>System prompt (advanced)</label>
              <textarea
                rows={8}
                className={`${ui.input} font-mono text-xs`}
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
              />
            </div>
            <div className="flex flex-wrap gap-3">
              <button
                type="button"
                disabled={savingSettings || (companyDescription === initialCompanyDescription && systemPrompt === initialSystemPrompt)}
                onClick={saveAgentSettings}
                className={ui.btnPrimary}
              >
                {savingSettings
                  ? "Saving…"
                  : (!initialCompanyDescription && !initialSystemPrompt)
                  ? "Save settings"
                  : "Update settings"}
              </button>
              <button
                type="button"
                disabled={resettingPrompt}
                onClick={resetAgentPrompt}
                className={ui.btnSecondary}
              >
                {resettingPrompt ? "Resetting…" : "Reset prompt for my company"}
              </button>
            </div>
          </div>
        </section>

        <SectionCard
          title="Inventory & POS"
          description={invCategory?.description}
          enabled={integrations.inventory.enabled}
          onEnabledChange={(v) =>
            setIntegrations({ ...integrations, inventory: { ...integrations.inventory, enabled: v } })
          }
        >
          <div className="space-y-6">
            {integrations.inventory.sources.map((src, idx) => (
              <div key={src.id} className="rounded-xl border border-gray-200 bg-gray-50/50 p-5 space-y-4">
                <div className="flex flex-wrap items-center gap-4">
                  <Toggle
                    checked={src.enabled}
                    onChange={(v) => {
                      const sources = [...integrations.inventory.sources];
                      sources[idx] = { ...src, enabled: v };
                      setIntegrations({ ...integrations, inventory: { ...integrations.inventory, sources } });
                    }}
                    label="Active"
                  />
                  <div className="flex-1 min-w-[180px]">
                    <label className={ui.label}>Provider</label>
                    <select
                      className={ui.input}
                      value={src.provider}
                      onChange={(e) => {
                        const sources = [...integrations.inventory.sources];
                        sources[idx] = {
                          ...src,
                          provider: e.target.value,
                          config: defaultConfigForProvider(invCategory, e.target.value),
                        };
                        setIntegrations({ ...integrations, inventory: { ...integrations.inventory, sources } });
                      }}
                    >
                      {invCategory?.providers.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="w-24">
                    <label className={ui.label}>Priority</label>
                    <input
                      type="number"
                      className={ui.input}
                      value={src.priority}
                      onChange={(e) => {
                        const sources = [...integrations.inventory.sources];
                        sources[idx] = { ...src, priority: parseInt(e.target.value, 10) || 0 };
                        setIntegrations({ ...integrations, inventory: { ...integrations.inventory, sources } });
                      }}
                    />
                  </div>
                  <button type="button" className={`${ui.btnDanger} self-end mb-1`} onClick={() => {
                    const sources = integrations.inventory.sources.filter((_, i) => i !== idx);
                    setIntegrations({ ...integrations, inventory: { ...integrations.inventory, sources } });
                  }}>
                    Remove
                  </button>
                </div>

                <ConnectionFields
                  categoryId="inventory"
                  category={invCategory}
                  providerId={src.provider}
                  config={src.config}
                  onConfigChange={(cfg) => {
                    const sources = [...integrations.inventory.sources];
                    sources[idx] = { ...src, config: cfg };
                    setIntegrations({ ...integrations, inventory: { ...integrations.inventory, sources } });
                  }}
                  backendUrl={backendUrl}
                  getHeaders={getHeaders}
                  sourceId={src.id}
                  getDiscovery={getDiscovery}
                  setDiscovery={setDiscovery}
                  setMessage={setMessage}
                />

                <div className="flex flex-wrap items-center gap-3 pt-2">
                  <button type="button" onClick={save} disabled={saving} className={ui.btnPrimary}>
                    {saving ? "Saving…" : "Save integration"}
                  </button>
                  <button
                    type="button"
                    disabled={testing === `inventory-${src.id}`}
                    onClick={() => testSource("inventory", src.provider, src.config, src.id)}
                    className={ui.btnSecondary}
                  >
                    {testing === `inventory-${src.id}` ? "Testing…" : "Test connection"}
                  </button>
                </div>
              </div>
            ))}

            <button
              type="button"
              className={ui.btnSecondary}
              disabled={!invCategory}
              title={invCategory ? undefined : "Provider definitions have not loaded yet"}
              onClick={() => {
                if (!invCategory) return;
                const id = `src_${Date.now()}`;
                setIntegrations({
                  ...integrations,
                  inventory: {
                    ...integrations.inventory,
                    sources: [
                      ...integrations.inventory.sources,
                      {
                        id,
                        enabled: true,
                        provider: "postgres",
                        priority: integrations.inventory.sources.length,
                        label: "postgres",
                        config: defaultConfigForProvider(invCategory, "postgres"),
                      },
                    ],
                  },
                });
              }}
            >
              + Add inventory source
            </button>
          </div>
        </SectionCard>

        {crmCategory && (
          <SectionCard
            title="CRM"
            description={crmCategory.description}
            enabled={integrations.crm.enabled}
            onEnabledChange={(v) => setIntegrations({ ...integrations, crm: { ...integrations.crm, enabled: v } })}
          >
            <div className="max-w-md mb-4">
              <label className={ui.label}>Provider</label>
              <select
                className={ui.input}
                value={integrations.crm.provider}
                onChange={(e) =>
                  setIntegrations({
                    ...integrations,
                    crm: {
                      ...integrations.crm,
                      provider: e.target.value,
                      config: defaultConfigForProvider(crmCategory, e.target.value),
                    },
                  })
                }
              >
                {crmCategory.providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>

            <ConnectionFields
              categoryId="crm"
              category={crmCategory}
              providerId={integrations.crm.provider}
              config={integrations.crm.config}
              onConfigChange={(cfg) => setIntegrations({ ...integrations, crm: { ...integrations.crm, config: cfg } })}
              backendUrl={backendUrl}
              getHeaders={getHeaders}
              getDiscovery={getDiscovery}
              setDiscovery={setDiscovery}
              setMessage={setMessage}
            />

            <div className="flex flex-wrap items-center gap-3 mt-4">
              <button type="button" onClick={save} disabled={saving} className={ui.btnPrimary}>
                {saving ? "Saving…" : "Save integration"}
              </button>
              <button
                type="button"
                disabled={testing === "crm-crm"}
                onClick={() => testSource("crm", integrations.crm.provider, integrations.crm.config)}
                className={ui.btnSecondary}
              >
                {testing === "crm-crm" ? "Testing…" : "Test connection"}
              </button>
            </div>
          </SectionCard>
        )}

        {calCategory && (
          <SectionCard
            title="Calendar"
            description={calCategory.description}
            enabled={integrations.calendar.enabled}
            onEnabledChange={(v) =>
              setIntegrations({ ...integrations, calendar: { ...integrations.calendar, enabled: v } })
            }
          >
            <div className="max-w-md mb-4">
              <label className={ui.label}>Provider</label>
              <select
                className={ui.input}
                value={integrations.calendar.provider}
                onChange={(e) =>
                  setIntegrations({
                    ...integrations,
                    calendar: {
                      ...integrations.calendar,
                      provider: e.target.value,
                      config: defaultConfigForProvider(calCategory, e.target.value),
                    },
                  })
                }
              >
                {calCategory.providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>

            <ConnectionFields
              categoryId="calendar"
              category={calCategory}
              providerId={integrations.calendar.provider}
              config={integrations.calendar.config}
              onConfigChange={(cfg) => setIntegrations({ ...integrations, calendar: { ...integrations.calendar, config: cfg } })}
              backendUrl={backendUrl}
              getHeaders={getHeaders}
              getDiscovery={getDiscovery}
              setDiscovery={setDiscovery}
              setMessage={setMessage}
            />

            <div className="flex flex-wrap items-center gap-3 mt-4">
              <button type="button" onClick={save} disabled={saving} className={ui.btnPrimary}>
                {saving ? "Saving…" : "Save integration"}
              </button>
              <button
                type="button"
                disabled={testing === "calendar-cal"}
                onClick={() => testSource("calendar", integrations.calendar.provider, integrations.calendar.config)}
                className={ui.btnSecondary}
              >
                {testing === "calendar-cal" ? "Testing…" : "Test connection"}
              </button>
            </div>
          </SectionCard>
        )}

        <div className="flex flex-wrap gap-3 pt-2 pb-16 border-t border-gray-200">
          <button type="button" onClick={save} disabled={saving} className={ui.btnPrimary}>
            {saving ? "Saving…" : "Save changes"}
          </button>
          <button
            type="button"
            onClick={syncKnowledge}
            disabled={syncingKnowledge}
            className={ui.btnSecondary}
            title="Push mapped Production / Sets / PO tables into Adapter-Hub so the agent can answer experience questions"
          >
            {syncingKnowledge ? "Syncing…" : "Sync knowledge to agent"}
          </button>
          <button type="button" onClick={load} className={ui.btnSecondary}>
            Reload
          </button>
          <p className="w-full text-xs text-gray-500 mt-1">
            After mapping Production, Sets, or PO tables under Inventory, save then click{" "}
            <span className="font-medium text-gray-700">Sync knowledge to agent</span>. The agent also
            queries those tables live via <code className="text-[11px]">query_pos_database</code>.
          </p>
        </div>
      </div>

      {/* Sticky Floating Save Bar */}
      <div className="fixed bottom-4 right-4 sm:right-8 z-50 flex items-center gap-3 bg-slate-900/90 backdrop-blur text-white px-5 py-3 rounded-2xl shadow-xl border border-slate-700/80 animate-in fade-in slide-in-from-bottom-4">
        <span className="text-xs font-medium text-slate-300 hidden sm:inline">Integration settings</span>
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="rounded-xl bg-blue-600 px-4 py-2 text-xs font-bold text-white shadow-md hover:bg-blue-500 active:scale-95 disabled:opacity-50 transition-all"
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}

/**
 * F01: the dashboard re-renders on every streamed WebSocket token. This panel
 * was re-rendering with it, and because `getHeaders` was a fresh closure each
 * time, `load` was invalidated, the effect re-fired, and /api/admin/tenant
 * overwrote whatever you were typing. `getHeaders` is now stable on the parent
 * side; memoising here stops the re-render reaching the panel at all.
 */
export default React.memo(AdminIntegrations);
