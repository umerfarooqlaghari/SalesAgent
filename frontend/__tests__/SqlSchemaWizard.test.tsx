import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import SqlSchemaWizard, { type MappedTable } from "@/app/components/SqlSchemaWizard";

// F07/X12: re-scanning a database used to unconditionally replace mapped_tables
// (with a silent .slice(0,12)), discarding hours of column-level curation.
// This guards the fix: existing mappings must survive a re-scan untouched, and
// only genuinely new tables get appended.

const CURATED_TABLE: MappedTable = {
  id: "mt_existing",
  table: "products",
  label: "Storefront Products (curated)",
  enabled: true,
  search_columns: ["name"],
  columns: { name: "name", price: "price" },
};

function renderWizard(overrides: Partial<React.ComponentProps<typeof SqlSchemaWizard>> = {}) {
  const onConfigChange = vi.fn();
  const onDiscovered = vi.fn();
  const props: React.ComponentProps<typeof SqlSchemaWizard> = {
    category: "inventory",
    provider: "postgres",
    config: { table_map: { mapped_tables: [CURATED_TABLE] } },
    onConfigChange,
    backendUrl: "http://backend.local",
    getHeaders: () => ({ Authorization: "Bearer test" }),
    sourceId: "src_1",
    discoveryKey: "key_1",
    discovered: null,
    onDiscovered,
    onMessage: vi.fn(),
    ...overrides,
  };
  render(<SqlSchemaWizard {...props} />);
  return { onConfigChange, onDiscovered, props };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.stubGlobal("sessionStorage", {
    setItem: vi.fn(),
    getItem: vi.fn(),
    removeItem: vi.fn(),
  } as unknown as Storage);
});

describe("SqlSchemaWizard — re-scan merge behavior (F07)", () => {
  it("keeps the existing curated mapping untouched and only appends new tables", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          tables: [
            { name: "products", columns: [{ name: "name", type: "text" }] },
            { name: "orders", columns: [{ name: "id", type: "int" }] },
          ],
          suggested_mapped_tables: [
            // Suggestion for the ALREADY-mapped table must NOT overwrite it.
            {
              id: "suggested_products",
              table: "products",
              label: "Products (auto-suggested, should be ignored)",
              enabled: true,
              search_columns: ["id"],
              columns: { id: "id" },
            },
            // Genuinely new table — should be appended.
            {
              id: "suggested_orders",
              table: "orders",
              label: "Orders",
              enabled: true,
              search_columns: ["id"],
              columns: { id: "id" },
            },
          ],
          message: "Found 2 tables",
        }),
      })
    );

    const { onConfigChange } = renderWizard();

    fireEvent.click(screen.getByRole("button", { name: /scan again/i }));

    await waitFor(() => expect(onConfigChange).toHaveBeenCalled());

    const lastCall = onConfigChange.mock.calls[onConfigChange.mock.calls.length - 1][0];
    const mapped = lastCall.table_map.mapped_tables as MappedTable[];

    expect(mapped).toHaveLength(2);
    const products = mapped.find((m) => m.table === "products");
    const orders = mapped.find((m) => m.table === "orders");

    // Existing curation must be byte-for-byte preserved.
    expect(products).toEqual(CURATED_TABLE);
    // New table must have been added from the suggestion.
    expect(orders?.table).toBe("orders");
  });

  it("does not truncate to 12 tables when many new tables are discovered", async () => {
    const manySuggestions: MappedTable[] = Array.from({ length: 20 }, (_, i) => ({
      id: `mt_new_${i}`,
      table: `table_${i}`,
      label: `Table ${i}`,
      enabled: true,
      search_columns: [],
      columns: { id: "id" },
    }));

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          tables: manySuggestions.map((m) => ({ name: m.table, columns: [{ name: "id", type: "int" }] })),
          suggested_mapped_tables: manySuggestions,
          message: "Found 20 tables",
        }),
      })
    );

    const { onConfigChange } = renderWizard({
      config: { table_map: { mapped_tables: [] } },
    });

    fireEvent.click(screen.getByRole("button", { name: /connect & scan database/i }));

    await waitFor(() => expect(onConfigChange).toHaveBeenCalled());

    const lastCall = onConfigChange.mock.calls[onConfigChange.mock.calls.length - 1][0];
    const mapped = lastCall.table_map.mapped_tables as MappedTable[];
    expect(mapped.length).toBe(20);
  });
});
