"use client";

import React, { useMemo, useState } from "react";

const SQL_PROVIDERS = new Set(["postgres", "sqlserver", "mysql"]);

/** Shared light-theme tokens for integrations UI */
export const ui = {
  card: "bg-white border border-gray-200 rounded-xl shadow-sm",
  cardHeader: "px-5 py-4 border-b border-gray-100",
  label: "block text-sm font-medium text-gray-700 mb-1.5",
  hint: "text-xs text-gray-500 mt-1",
  input:
    "w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20",
  btnPrimary:
    "inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors",
  btnSecondary:
    "inline-flex items-center justify-center gap-2 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors",
  btnGhost: "text-sm font-medium text-gray-500 hover:text-gray-800 transition-colors",
  btnDanger: "text-sm font-medium text-red-600 hover:text-red-700",
  badge: "inline-flex items-center rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-600",
  badgeBlue: "inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700",
};

type ColumnInfo = { name: string; type: string };
type TableInfo = { name: string; columns: ColumnInfo[] };

export type MappedTable = {
  id: string;
  table: string;
  label: string;
  enabled: boolean;
  role?: string;
  search_columns: string[];
  columns: Record<string, string>;
};

type DiscoverResult = {
  tables: TableInfo[];
  suggested_table_map?: Record<string, unknown>;
  suggested_mapped_tables?: MappedTable[];
  message?: string;
};

export type { DiscoverResult, TableInfo };

function parseTableMap(config: Record<string, unknown>): Record<string, unknown> {
  const tm = config.table_map;
  if (!tm) return {};
  if (typeof tm === "string") {
    try {
      return JSON.parse(tm);
    } catch {
      return {};
    }
  }
  return tm as Record<string, unknown>;
}

function columnsForTable(tables: TableInfo[], tableName: string): ColumnInfo[] {
  return tables.find((t) => t.name === tableName)?.columns || [];
}

function migrateLegacyMapped(category: string, tableMap: Record<string, unknown>): MappedTable[] {
  const raw = tableMap.mapped_tables;
  if (Array.isArray(raw) && raw.length) return raw as MappedTable[];

  const out: MappedTable[] = [];
  if (category === "crm" && tableMap.companies_table) {
    const cols = (tableMap.companies_columns as Record<string, string>) || {};
    const search = cols.company ? [cols.company] : Object.values(cols).slice(0, 1);
    out.push({
      id: "legacy_crm",
      table: String(tableMap.companies_table),
      label: String(tableMap.companies_table),
      enabled: true,
      search_columns: search,
      columns: cols,
    });
  }
  return out;
}

function newMappedFromTable(tableName: string, columns: ColumnInfo[]): MappedTable {
  const colMap: Record<string, string> = {};
  const search: string[] = [];
  for (const c of columns.slice(0, 8)) {
    colMap[c.name] = c.name;
  }
  const searchHint = columns.find((c) =>
    /name|company|email|title|label|description/i.test(c.name)
  );
  if (searchHint) search.push(searchHint.name);
  else if (columns[0]) search.push(columns[0].name);

  return {
    id: `mt_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    table: tableName,
    label: tableName.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase()),
    enabled: true,
    search_columns: search,
    columns: colMap,
  };
}

interface Props {
  category: string;
  provider: string;
  config: Record<string, unknown>;
  onConfigChange: (cfg: Record<string, unknown>) => void;
  backendUrl: string;
  getHeaders: () => Record<string, string>;
  sourceId?: string;
  discoveryKey: string;
  discovered: DiscoverResult | null;
  onDiscovered: (data: DiscoverResult | null) => void;
  onMessage?: (msg: string) => void;
}

function TableLibrary({
  tables,
  mappedTables,
  filter,
  onFilterChange,
  onAdd,
}: {
  tables: TableInfo[];
  mappedTables: MappedTable[];
  filter: string;
  onFilterChange: (v: string) => void;
  onAdd: (name: string) => void;
}) {
  const mappedSet = new Set(mappedTables.map((m) => m.table));
  const q = filter.trim().toLowerCase();
  const filtered = tables.filter(
    (t) => !mappedSet.has(t.name) && (!q || t.name.toLowerCase().includes(q))
  );

  return (
    <div className={`${ui.card} overflow-hidden flex flex-col h-full min-h-[280px]`}>
      <div className={ui.cardHeader}>
        <h4 className="text-sm font-semibold text-gray-900">Available tables</h4>
        <p className={ui.hint}>Click a table to add it to your agent</p>
        <input
          type="search"
          placeholder="Search tables…"
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
          className={`${ui.input} mt-3`}
        />
      </div>
      <div className="flex-1 overflow-y-auto max-h-[min(24rem,50vh)] divide-y divide-gray-100">
        {filtered.length === 0 ? (
          <p className="p-4 text-sm text-gray-500 text-center">
            {tables.length === mappedSet.size ? "All tables have been added." : "No tables match your search."}
          </p>
        ) : (
          filtered.map((t) => (
            <button
              key={t.name}
              type="button"
              onClick={() => onAdd(t.name)}
              className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left hover:bg-blue-50/80 transition-colors group"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium text-gray-900 font-mono truncate">{t.name}</p>
                <p className="text-xs text-gray-500">{t.columns.length} columns</p>
              </div>
              <span className="shrink-0 rounded-md bg-blue-600 px-2.5 py-1 text-xs font-semibold text-white opacity-0 group-hover:opacity-100 transition-opacity">
                Add
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

function MappedTableCard({
  entry,
  tables,
  expanded,
  onToggleExpand,
  onUpdate,
  onRemove,
  onToggleColumn,
  onToggleSearch,
  onSelectAllColumns,
}: {
  entry: MappedTable;
  tables: TableInfo[];
  expanded: boolean;
  onToggleExpand: () => void;
  onUpdate: (patch: Partial<MappedTable>) => void;
  onRemove: () => void;
  onToggleColumn: (col: string, on: boolean) => void;
  onToggleSearch: (col: string, on: boolean) => void;
  onSelectAllColumns: (on: boolean) => void;
}) {
  const dbCols = columnsForTable(tables, entry.table);
  const colCount = Object.keys(entry.columns).length;
  const searchCount = entry.search_columns.length;
  const [colFilter, setColFilter] = useState("");

  const filteredCols = dbCols.filter(
    (c) => !colFilter.trim() || c.name.toLowerCase().includes(colFilter.trim().toLowerCase())
  );

  return (
    <div className={`${ui.card} overflow-hidden ${!entry.enabled ? "opacity-60" : ""}`}>
      <div
        className="flex items-start gap-3 px-4 py-3 cursor-pointer hover:bg-gray-50/80"
        onClick={onToggleExpand}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && onToggleExpand()}
      >
        <div className="pt-0.5">
          <input
            type="checkbox"
            checked={entry.enabled}
            onChange={(e) => {
              e.stopPropagation();
              onUpdate({ enabled: e.target.checked });
            }}
            className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            title="Enable this table for the agent"
          />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-gray-900">{entry.label || entry.table}</span>
            <span className={ui.badge}>{entry.table}</span>
            <span className={ui.badgeBlue}>
              {colCount} column{colCount !== 1 ? "s" : ""}
            </span>
            {searchCount > 0 && (
              <span className="inline-flex items-center rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800">
                {searchCount} searchable
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className={ui.btnDanger}
        >
          Remove
        </button>
        <svg
          className={`h-5 w-5 text-gray-400 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </div>

      {expanded && (
        <div className="border-t border-gray-100 px-4 py-4 space-y-4 bg-gray-50/50">
          <div className="grid sm:grid-cols-2 gap-4">
            <div>
              <label className={ui.label}>Display name</label>
              <input
                className={ui.input}
                value={entry.label}
                placeholder="e.g. Customer records"
                onChange={(e) => onUpdate({ label: e.target.value })}
              />
            </div>
            <div>
              <label className={ui.label}>Database table</label>
              <select
                className={ui.input}
                value={entry.table}
                onChange={(e) => {
                  const name = e.target.value;
                  const cols = columnsForTable(tables, name);
                  onUpdate(newMappedFromTable(name, cols));
                }}
              >
                {tables.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
              <div>
                <p className="text-sm font-semibold text-gray-900">Columns</p>
                <p className={ui.hint}>
                  Choose what the agent can read. Turn on <strong>Search</strong> for columns used to find records.
                </p>
              </div>
              <div className="flex gap-2">
                <button type="button" className={ui.btnSecondary} onClick={() => onSelectAllColumns(true)}>
                  Select all
                </button>
                <button type="button" className={ui.btnSecondary} onClick={() => onSelectAllColumns(false)}>
                  Clear
                </button>
              </div>
            </div>

            <input
              type="search"
              placeholder="Filter columns…"
              value={colFilter}
              onChange={(e) => setColFilter(e.target.value)}
              className={`${ui.input} mb-2 max-w-xs`}
            />

            <div className="rounded-lg border border-gray-200 bg-white overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    <th className="px-4 py-2.5 w-12">Include</th>
                    <th className="px-4 py-2.5 w-12">Search</th>
                    <th className="px-4 py-2.5">Column</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Type</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {filteredCols.map((col) => {
                    const included = Object.values(entry.columns).includes(col.name);
                    const isSearch = entry.search_columns.includes(col.name);
                    return (
                      <tr key={col.name} className={included ? "bg-blue-50/30" : "hover:bg-gray-50/50"}>
                        <td className="px-4 py-2">
                          <input
                            type="checkbox"
                            checked={included}
                            onChange={(e) => onToggleColumn(col.name, e.target.checked)}
                            className="h-4 w-4 rounded border-gray-300 text-blue-600"
                          />
                        </td>
                        <td className="px-4 py-2">
                          <input
                            type="checkbox"
                            disabled={!included}
                            checked={isSearch}
                            onChange={(e) => onToggleSearch(col.name, e.target.checked)}
                            className="h-4 w-4 rounded border-gray-300 text-amber-600 disabled:opacity-30"
                            title="Use this column when searching"
                          />
                        </td>
                        <td className="px-4 py-2 font-mono text-gray-800">{col.name}</td>
                        <td className="px-4 py-2 text-gray-500 hidden sm:table-cell">{col.type}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SqlSchemaWizard({
  category,
  provider,
  config,
  onConfigChange,
  backendUrl,
  getHeaders,
  sourceId,
  discoveryKey,
  discovered,
  onDiscovered,
  onMessage,
}: Props) {
  const [scanning, setScanning] = useState(false);
  const [tableFilter, setTableFilter] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const tableMap = useMemo(() => parseTableMap(config), [config]);
  const tables = discovered?.tables ?? [];
  const mappedTables = useMemo(() => migrateLegacyMapped(category, tableMap), [category, tableMap]);
  const hasScan = tables.length > 0;

  const updateMappedTables = (next: MappedTable[]) => {
    onConfigChange({ ...config, table_map: { ...tableMap, mapped_tables: next } });
  };

  const scanDatabase = async () => {
    setScanning(true);
    onMessage?.("");
    try {
      const res = await fetch(`${backendUrl}/api/admin/integrations/discover-schema`, {
        method: "POST",
        headers: getHeaders(),
        body: JSON.stringify({ category, provider, config, source_id: sourceId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Could not scan database");

      onDiscovered(data as DiscoverResult);
      try {
        sessionStorage.setItem(`alpha-discovery-${discoveryKey}`, JSON.stringify(data));
      } catch {
        /* ignore quota */
      }

      const suggested = (data.suggested_mapped_tables || []) as MappedTable[];
      const fallback =
        suggested.length > 0
          ? suggested.filter((t) => t.enabled).slice(0, 3)
          : migrateLegacyMapped(category, { ...tableMap, ...(data.suggested_table_map || {}) });

      onConfigChange({
        ...config,
        table_map: { ...tableMap, ...(data.suggested_table_map || {}), mapped_tables: fallback },
      });
      if (fallback[0]) setExpandedId(fallback[0].id);
      onMessage?.(data.message || `Found ${data.tables?.length || 0} tables in your database.`);
    } catch (e: unknown) {
      onMessage?.(e instanceof Error ? e.message : "Scan failed");
    } finally {
      setScanning(false);
    }
  };

  const addTable = (tableName: string) => {
    if (!tableName || mappedTables.some((m) => m.table === tableName)) return;
    const entry = newMappedFromTable(tableName, columnsForTable(tables, tableName));
    updateMappedTables([...mappedTables, entry]);
    setExpandedId(entry.id);
  };

  const updateEntry = (id: string, patch: Partial<MappedTable>) => {
    updateMappedTables(mappedTables.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  };

  const removeEntry = (id: string) => {
    updateMappedTables(mappedTables.filter((m) => m.id !== id));
    if (expandedId === id) setExpandedId(null);
  };

  const toggleColumn = (entry: MappedTable, colName: string, on: boolean) => {
    const cols = { ...entry.columns };
    if (on) cols[colName] = colName;
    else delete cols[colName];
    const search = entry.search_columns.filter((s) => Object.values(cols).includes(s));
    updateEntry(entry.id, { columns: cols, search_columns: search });
  };

  const toggleSearchColumn = (entry: MappedTable, physicalCol: string, on: boolean) => {
    let search = [...entry.search_columns];
    if (on && !search.includes(physicalCol)) search.push(physicalCol);
    if (!on) search = search.filter((s) => s !== physicalCol);
    updateEntry(entry.id, { search_columns: search });
  };

  const selectAllColumns = (entry: MappedTable, on: boolean) => {
    const dbCols = columnsForTable(tables, entry.table);
    if (!on) {
      updateEntry(entry.id, { columns: {}, search_columns: [] });
      return;
    }
    const cols: Record<string, string> = {};
    for (const c of dbCols) cols[c.name] = c.name;
    updateEntry(entry.id, { columns: cols });
  };

  if (!SQL_PROVIDERS.has(provider)) return null;

  const step = !hasScan ? 1 : mappedTables.length === 0 ? 2 : 3;

  return (
    <div className="mt-6 space-y-5">
      {/* Step indicator */}
      <div className="flex items-center gap-2 text-xs font-medium">
        {[
          { n: 1, label: "Connect" },
          { n: 2, label: "Scan" },
          { n: 3, label: "Map tables" },
        ].map((s, i) => (
          <React.Fragment key={s.n}>
            {i > 0 && <div className="h-px w-6 bg-gray-200" />}
            <span
              className={`flex items-center gap-1.5 rounded-full px-3 py-1 ${
                step >= s.n ? "bg-blue-100 text-blue-800" : "bg-gray-100 text-gray-500"
              }`}
            >
              <span
                className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold ${
                  step >= s.n ? "bg-blue-600 text-white" : "bg-gray-300 text-white"
                }`}
              >
                {step > s.n ? "✓" : s.n}
              </span>
              {s.label}
            </span>
          </React.Fragment>
        ))}
      </div>

      {!hasScan ? (
        <div className={`${ui.card} p-5`}>
          <h4 className="text-base font-semibold text-gray-900">Scan your database</h4>
          <p className={`${ui.hint} mt-1 max-w-xl`}>
            Enter connection details above, then scan. We&apos;ll list every table and column so you can choose what
            the agent can access.
          </p>
          <button type="button" disabled={scanning} onClick={scanDatabase} className={`${ui.btnPrimary} mt-4`}>
            {scanning ? (
              <>
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                Scanning…
              </>
            ) : (
              "Connect & scan database"
            )}
          </button>
        </div>
      ) : (
        <>
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-emerald-900">
                {tables.length} table{tables.length !== 1 ? "s" : ""} found
              </p>
              <p className="text-xs text-emerald-700 mt-0.5">
                Add tables on the left, then choose columns on the right.
              </p>
            </div>
            <button type="button" disabled={scanning} onClick={scanDatabase} className={ui.btnSecondary}>
              {scanning ? "Scanning…" : "Scan again"}
            </button>
          </div>

          <div className="grid lg:grid-cols-2 gap-5 items-start">
            <TableLibrary
              tables={tables}
              mappedTables={mappedTables}
              filter={tableFilter}
              onFilterChange={setTableFilter}
              onAdd={addTable}
            />

            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h4 className="text-sm font-semibold text-gray-900">
                  Your selections
                  <span className="ml-2 font-normal text-gray-500">({mappedTables.length})</span>
                </h4>
              </div>

              {mappedTables.length === 0 ? (
                <div className={`${ui.card} p-8 text-center`}>
                  <p className="text-sm font-medium text-gray-700">No tables selected yet</p>
                  <p className={`${ui.hint} mt-1`}>Click a table on the left to add it here.</p>
                </div>
              ) : (
                mappedTables.map((entry) => (
                  <MappedTableCard
                    key={entry.id}
                    entry={entry}
                    tables={tables}
                    expanded={expandedId === entry.id}
                    onToggleExpand={() => setExpandedId(expandedId === entry.id ? null : entry.id)}
                    onUpdate={(patch) => updateEntry(entry.id, patch)}
                    onRemove={() => removeEntry(entry.id)}
                    onToggleColumn={(col, on) => toggleColumn(entry, col, on)}
                    onToggleSearch={(col, on) => toggleSearchColumn(entry, col, on)}
                    onSelectAllColumns={(on) => selectAllColumns(entry, on)}
                  />
                ))
              )}
            </div>
          </div>
        </>
      )}

      <details className="group">
        <summary className={`${ui.btnGhost} cursor-pointer list-none`}>
          Advanced: edit raw JSON mapping
        </summary>
        <textarea
          rows={8}
          className={`${ui.input} mt-2 font-mono text-xs`}
          value={JSON.stringify({ ...tableMap, mapped_tables: mappedTables }, null, 2)}
          onChange={(e) => {
            try {
              onConfigChange({ ...config, table_map: JSON.parse(e.target.value) });
            } catch {
              onConfigChange({ ...config, table_map: e.target.value });
            }
          }}
        />
      </details>
    </div>
  );
}

export { SQL_PROVIDERS };
