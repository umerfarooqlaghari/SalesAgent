import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SqlSchemaWizard, { type MappedTable } from "@/app/components/SqlSchemaWizard";

/**
 * X12 / F02: the raw-JSON editor wrote through on every keystroke, and on a
 * parse failure it wrote the raw *string* into `table_map`. `parseTableMap`
 * could not parse that string, returned `{}`, and every mapped table vanished
 * — from the UI and from whatever Save persisted next.
 *
 * F16: un-checking a column deleted by key while the checkbox tested the
 * values, so aliased (suggested / legacy) mappings snapped straight back on.
 */

const CURATED: MappedTable = {
  id: "mt_existing",
  table: "products",
  label: "Storefront Products",
  enabled: true,
  search_columns: ["name"],
  columns: { name: "name", price: "price" },
};

/** A suggested-style entry: the map KEY differs from the physical column. */
const ALIASED: MappedTable = {
  id: "mt_aliased",
  table: "products",
  label: "Products",
  enabled: true,
  search_columns: ["product_name"],
  columns: { name: "product_name", cost: "price" },
};

const DISCOVERED = {
  tables: [
    {
      name: "products",
      columns: [
        { name: "product_name", type: "text" },
        { name: "price", type: "numeric" },
      ],
    },
  ],
};

function renderWizard(
  overrides: Partial<React.ComponentProps<typeof SqlSchemaWizard>> = {}
) {
  const onConfigChange = vi.fn();
  const props: React.ComponentProps<typeof SqlSchemaWizard> = {
    category: "inventory",
    provider: "postgres",
    config: { table_map: { mapped_tables: [CURATED] } },
    onConfigChange,
    backendUrl: "http://backend.local",
    getHeaders: () => ({ Authorization: "Bearer test" }),
    sourceId: "src_1",
    discoveryKey: "key_1",
    discovered: null,
    onDiscovered: vi.fn(),
    onMessage: vi.fn(),
    ...overrides,
  };
  const view = render(<SqlSchemaWizard {...props} />);
  return { onConfigChange, view, props };
}

beforeEach(() => {
  vi.stubGlobal("sessionStorage", {
    length: 0,
    key: vi.fn(() => null),
    setItem: vi.fn(),
    getItem: vi.fn(),
    removeItem: vi.fn(),
  } as unknown as Storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SqlSchemaWizard — raw JSON editor (F02 / X12)", () => {
  it("does not touch the config while the JSON is invalid", () => {
    const { onConfigChange } = renderWizard();
    const textarea = screen.getByLabelText("Raw JSON mapping");

    // One stray character, exactly the reported data-loss trigger.
    fireEvent.change(textarea, { target: { value: '{ "mapped_tables": [ }' } });
    expect(onConfigChange).not.toHaveBeenCalled();

    // Blur used to be irrelevant — it wrote on change. Now it validates.
    fireEvent.blur(textarea);
    expect(onConfigChange).not.toHaveBeenCalled();
    expect(screen.getByText(/Your existing mapping is unchanged/i)).toBeInTheDocument();
  });

  it("never writes a bare string into table_map", () => {
    const { onConfigChange } = renderWizard();
    const textarea = screen.getByLabelText("Raw JSON mapping");

    fireEvent.change(textarea, { target: { value: '"just a string"' } });
    fireEvent.blur(textarea);

    expect(onConfigChange).not.toHaveBeenCalled();

    fireEvent.change(textarea, { target: { value: "[1, 2, 3]" } });
    fireEvent.blur(textarea);

    expect(onConfigChange).not.toHaveBeenCalled();
  });

  it("commits once the JSON is valid again", () => {
    const { onConfigChange } = renderWizard();
    const textarea = screen.getByLabelText("Raw JSON mapping");

    fireEvent.change(textarea, { target: { value: '{ "mapped_tables": [ }' } });
    fireEvent.blur(textarea);
    expect(onConfigChange).not.toHaveBeenCalled();

    const fixed = JSON.stringify({ mapped_tables: [CURATED], note: "edited" });
    fireEvent.change(textarea, { target: { value: fixed } });
    fireEvent.blur(textarea);

    expect(onConfigChange).toHaveBeenCalledTimes(1);
    const written = onConfigChange.mock.calls[0][0];
    expect(written.table_map.mapped_tables).toEqual([CURATED]);
    expect(written.table_map.note).toBe("edited");
  });

  it("offers Discard to get back to the committed mapping", () => {
    const { onConfigChange } = renderWizard();
    const textarea = screen.getByLabelText("Raw JSON mapping") as HTMLTextAreaElement;
    const before = textarea.value;

    fireEvent.change(textarea, { target: { value: "nonsense" } });
    fireEvent.click(screen.getByRole("button", { name: /discard/i }));

    expect(textarea.value).toBe(before);
    expect(onConfigChange).not.toHaveBeenCalled();
  });
});

describe("SqlSchemaWizard — aliased column un-check (F16)", () => {
  it("removes the alias whose value matches the physical column", () => {
    const { onConfigChange } = renderWizard({
      config: { table_map: { mapped_tables: [ALIASED] } },
      discovered: DISCOVERED,
    });

    // Open the card so the column table renders.
    fireEvent.click(screen.getByText("Products"));

    const rows = screen.getAllByRole("row");
    const productNameRow = rows.find((r) => r.textContent?.includes("product_name"));
    expect(productNameRow).toBeTruthy();

    const includeBox = productNameRow!.querySelectorAll("input[type=checkbox]")[0];
    expect((includeBox as HTMLInputElement).checked).toBe(true);

    fireEvent.click(includeBox);

    expect(onConfigChange).toHaveBeenCalled();
    const written = onConfigChange.mock.calls.at(-1)![0];
    const entry = (written.table_map.mapped_tables as MappedTable[])[0];

    // The alias `name -> product_name` must be gone...
    expect(Object.values(entry.columns)).not.toContain("product_name");
    // ...and the unrelated alias must survive.
    expect(Object.values(entry.columns)).toContain("price");
    // The search column that pointed at it must be dropped too.
    expect(entry.search_columns).not.toContain("product_name");
  });
});

describe("SqlSchemaWizard — enable checkbox (F15)", () => {
  it("does not expand the card when the enable box is ticked", () => {
    renderWizard({ discovered: DISCOVERED });

    // The card is collapsed: no column table yet.
    expect(screen.queryByText("Include")).not.toBeInTheDocument();

    const enableBox = screen
      .getAllByTitle("Enable this table for the agent")[0] as HTMLInputElement;
    fireEvent.click(enableBox);

    expect(screen.queryByText("Include")).not.toBeInTheDocument();
  });
});

describe("SqlSchemaWizard — discovery is not persisted (F22)", () => {
  it("keeps the scanned production schema out of sessionStorage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ ...DISCOVERED, suggested_mapped_tables: [], message: "ok" }),
      })
    );

    const { props } = renderWizard({ config: { table_map: { mapped_tables: [] } } });
    fireEvent.click(screen.getByRole("button", { name: /connect & scan database/i }));

    await vi.waitFor(() => expect(props.onDiscovered).toHaveBeenCalled());
    expect(sessionStorage.setItem).not.toHaveBeenCalled();
  });
});
