"use client";

import React, { useCallback, useEffect, useState } from "react";
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

function defaultConfigForProvider(category: CategorySchema, providerId: string): Record<string, unknown> {
  const provider = category.providers.find((p) => p.id === providerId);
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

export default function AdminIntegrations({ backendUrl, getHeaders }: Props) {
  const [schemas, setSchemas] = useState<CategorySchema[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationsState | null>(null);
  const [tenantId, setTenantId] = useState("");
  const [orgName, setOrgName] = useState("");
  const [companyDescription, setCompanyDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [resettingPrompt, setResettingPrompt] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [status, setStatus] = useState("");
  const [statusOk, setStatusOk] = useState<boolean | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [discoveryCache, setDiscoveryCache] = useState<Record<string, DiscoverResult>>({});

  const setDiscovery = (key: string, data: DiscoverResult | null) => {
    setDiscoveryCache((prev) => {
      const next = { ...prev };
      if (data) next[key] = data;
      else delete next[key];
      return next;
    });
    if (data) {
      try {
        sessionStorage.setItem(`alpha-discovery-${key}`, JSON.stringify(data));
      } catch {
        /* ignore */
      }
    }
  };

  const getDiscovery = (key: string): DiscoverResult | null => {
    if (discoveryCache[key]) return discoveryCache[key];
    try {
      const raw = sessionStorage.getItem(`alpha-discovery-${key}`);
      if (raw) return JSON.parse(raw) as DiscoverResult;
    } catch {
      /* ignore */
    }
    return null;
  };

  const setMessage = (msg: string, ok: boolean | null = null) => {
    setStatus(msg);
    setStatusOk(ok ?? (msg.toLowerCase().includes("ok") || msg.toLowerCase().includes("success") || msg.toLowerCase().includes("found") ? true : msg ? false : null));
  };

  const load = useCallback(async () => {
    try {
      const [schemaRes, tenantRes] = await Promise.all([
        fetch(`${backendUrl}/api/admin/integration-schemas`, { headers: getHeaders() }),
        fetch(`${backendUrl}/api/admin/tenant`, { headers: getHeaders() }),
      ]);
      if (schemaRes.ok) {
        const data = await schemaRes.json();
        setSchemas(data.categories || []);
      }
      if (tenantRes.ok) {
        const data = await tenantRes.json();
        setTenantId(data.tenant_id || "");
        setOrgName(data.org_name || "");
        setIntegrations(data.integrations);
        setCompanyDescription(data.settings?.company_description || "");
        setSystemPrompt(data.settings?.system_prompt || "");
      }
    } catch (e) {
      console.error(e);
      setMessage("Failed to load integration settings.", false);
    }
  }, [backendUrl, getHeaders]);

  useEffect(() => {
    load();
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
      if (data.settings?.system_prompt) setSystemPrompt(data.settings.system_prompt);
      setMessage("Settings saved successfully.", true);
    } catch (e: unknown) {
      setMessage(e instanceof Error ? e.message : "Save failed", false);
    } finally {
      setSaving(false);
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
      setCompanyDescription(data.settings?.company_description || "");
      setSystemPrompt(data.settings?.system_prompt || "");
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
      setSystemPrompt(data.settings?.system_prompt || "");
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

  if (!integrations) {
    return (
      <div className="flex flex-1 items-center justify-center bg-slate-50 min-h-[60vh]">
        <p className="text-sm text-gray-500">Loading integrations…</p>
      </div>
    );
  }

  const invCategory = schemas.find((c) => c.id === "inventory");
  const crmCategory = schemas.find((c) => c.id === "crm");
  const calCategory = schemas.find((c) => c.id === "calendar");

  const renderConnectionFields = (
    categoryId: string,
    category: CategorySchema | undefined,
    providerId: string,
    config: Record<string, unknown>,
    onConfigChange: (cfg: Record<string, unknown>) => void,
    sourceId?: string
  ) => {
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
  };

  const SectionCard = ({
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
  }) => (
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

  return (
    <div className="flex-1 overflow-y-auto bg-slate-50 min-h-full">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 py-8 space-y-6">
        <header className="border-b border-gray-200 pb-6">
          <h2 className="text-2xl font-semibold text-gray-900 tracking-tight">Integrations</h2>
          <p className="text-sm text-gray-500 mt-1">
            Connect your databases and services. Organization:{" "}
            <code className="rounded bg-gray-100 px-1.5 py-0.5 text-xs font-mono text-gray-700">{tenantId}</code>
          </p>
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
                disabled={savingSettings}
                onClick={saveAgentSettings}
                className={ui.btnPrimary}
              >
                {savingSettings ? "Saving…" : "Save agent settings"}
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
                          config: defaultConfigForProvider(invCategory!, e.target.value),
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

                {renderConnectionFields("inventory", invCategory, src.provider, src.config, (cfg) => {
                  const sources = [...integrations.inventory.sources];
                  sources[idx] = { ...src, config: cfg };
                  setIntegrations({ ...integrations, inventory: { ...integrations.inventory, sources } });
                }, src.id)}

                <button
                  type="button"
                  disabled={testing === `inventory-${src.id}`}
                  onClick={() => testSource("inventory", src.provider, src.config, src.id)}
                  className={ui.btnSecondary}
                >
                  {testing === `inventory-${src.id}` ? "Testing…" : "Test connection"}
                </button>
              </div>
            ))}

            <button
              type="button"
              className={ui.btnSecondary}
              onClick={() => {
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
                        config: defaultConfigForProvider(invCategory!, "postgres"),
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

            {renderConnectionFields(
              "crm",
              crmCategory,
              integrations.crm.provider,
              integrations.crm.config,
              (cfg) => setIntegrations({ ...integrations, crm: { ...integrations.crm, config: cfg } })
            )}

            <button
              type="button"
              disabled={testing === "crm-crm"}
              onClick={() => testSource("crm", integrations.crm.provider, integrations.crm.config)}
              className={`${ui.btnSecondary} mt-4`}
            >
              {testing === "crm-crm" ? "Testing…" : "Test connection"}
            </button>
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

            {renderConnectionFields(
              "calendar",
              calCategory,
              integrations.calendar.provider,
              integrations.calendar.config,
              (cfg) => setIntegrations({ ...integrations, calendar: { ...integrations.calendar, config: cfg } })
            )}

            <button
              type="button"
              disabled={testing === "calendar-cal"}
              onClick={() => testSource("calendar", integrations.calendar.provider, integrations.calendar.config)}
              className={`${ui.btnSecondary} mt-4`}
            >
              {testing === "calendar-cal" ? "Testing…" : "Test connection"}
            </button>
          </SectionCard>
        )}

        <div className="flex flex-wrap gap-3 pt-2 pb-10 border-t border-gray-200">
          <button type="button" onClick={save} disabled={saving} className={ui.btnPrimary}>
            {saving ? "Saving…" : "Save changes"}
          </button>
          <button type="button" onClick={load} className={ui.btnSecondary}>
            Reload
          </button>
        </div>
      </div>
    </div>
  );
}
