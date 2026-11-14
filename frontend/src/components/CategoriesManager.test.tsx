import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { Category, CategoryUsage } from "../api/types";
import { CategoriesManager } from "./CategoriesManager";

/**
 * Settings → Categories. What has to hold: renaming, adding and removing each call the
 * API with exactly what the person chose; removing says what it will touch BEFORE it
 * happens and lets them choose where the transactions go; the two categories the app
 * files into itself, and a group with categories in it, offer no Remove at all; and a
 * read-only member sees no buttons.
 */

const role = vi.hoisted(() => ({ value: "owner" }));
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ me: { user: { role: role.value } } }),
}));

let n = 0;
function cat(name: string, parent: Category | null, extra: Partial<Category> = {}): Category {
  n += 1;
  return {
    id: `c${n}`,
    name,
    parent_id: parent?.id ?? null,
    kind: parent?.kind ?? "expense",
    icon: null,
    color: null,
    is_system: true,
    sort: n,
    system_key: name,
    protected: false,
    ...extra,
  };
}

const food = cat("Food & Drink (out)", null);
const coffee = cat("Coffee", food);
const takeaway = cat("Takeaway", food);
const transportGroup = cat("Transport", null);
const fuel = cat("Fuel", transportGroup);
const transfers = cat("Transfers", null, { kind: "transfer" });
const internal = cat("Internal transfers", transfers, { protected: true });
const bucket = cat("Uncategorised", null, { protected: true });
const empty = cat("Boat", null, { is_system: false, system_key: null });
const ALL = [food, coffee, takeaway, transportGroup, fuel, transfers, internal, bucket, empty];

const USAGE: CategoryUsage = { transactions: 2, rules: 1, budgets: 1, subcategories: 0 };

function show() {
  vi.spyOn(api, "categories").mockResolvedValue(ALL);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CategoriesManager />
    </QueryClientProvider>,
  );
}

const button = (name: string | RegExp) => screen.getByRole("button", { name });

beforeEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  role.value = "owner";
});

describe("the list", () => {
  it("shows each group with how many categories it holds", async () => {
    show();

    await screen.findByText("Coffee");
    const summaries = Array.from(document.querySelectorAll("summary")).map((s) => s.textContent);

    expect(summaries).toContain("Food & Drink (out) · 2 categories");
    expect(summaries).toContain("Transfers · 1 category · Transfers");
    expect(summaries).toContain("Boat · 0 categories");
  });

  it("narrows to what matches, by category or by group", async () => {
    show();
    await screen.findByText("Coffee");
    fireEvent.change(screen.getByLabelText("Find a category"), { target: { value: "fuel" } });

    const section = screen.getByRole("region", { name: "Transport" });
    expect(within(section).getByText("Fuel")).toBeInTheDocument();
    expect(screen.queryByText("Coffee")).not.toBeInTheDocument();
  });
});

describe("renaming", () => {
  it("renames with the name typed, and says so", async () => {
    const rename = vi.spyOn(api, "renameCategory").mockResolvedValue({ ...fuel, name: "Petrol" });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Fuel" }));

    const input = screen.getByLabelText("New name for Fuel");
    expect(input).toHaveValue("Fuel");
    fireEvent.change(input, { target: { value: "Petrol" } });
    fireEvent.click(button("Save"));

    await waitFor(() => expect(rename).toHaveBeenCalledWith(fuel.id, "Petrol"));
    expect(await screen.findByRole("status")).toHaveTextContent("Renamed Fuel to Petrol.");
  });

  it("shows why the app refused a name", async () => {
    vi.spyOn(api, "renameCategory").mockRejectedValue(new Error("There is already a category called Coffee"));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Fuel" }));
    fireEvent.change(screen.getByLabelText("New name for Fuel"), { target: { value: "coffee" } });
    fireEvent.click(button("Save"));

    expect(await screen.findByRole("alert")).toHaveTextContent("There is already a category called Coffee");
  });

  it("backs out with Escape and changes nothing", async () => {
    const rename = vi.spyOn(api, "renameCategory");
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Fuel" }));
    fireEvent.change(screen.getByLabelText("New name for Fuel"), { target: { value: "Petrol" } });
    fireEvent.keyDown(screen.getByLabelText("New name for Fuel"), { key: "Escape" });

    expect(screen.queryByLabelText("New name for Fuel")).not.toBeInTheDocument();
    expect(rename).not.toHaveBeenCalled();
  });

  it("can rename the categories the app relies on, since the app finds them by key", async () => {
    show();
    expect(await screen.findByRole("button", { name: "Rename Internal transfers" })).toBeInTheDocument();
    expect(button("Rename the Uncategorised group")).toBeInTheDocument();
  });
});

describe("removing", () => {
  it("says what it will touch before anything happens, then moves them where chosen", async () => {
    vi.spyOn(api, "categoryUsage").mockResolvedValue(USAGE);
    const remove = vi
      .spyOn(api, "removeCategory")
      .mockResolvedValue({ moved_transactions: 2, moved_rules: 1, removed_rules: 0, removed_budgets: 1 });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Coffee" }));

    expect(await screen.findByText("Coffee is used by 2 transactions, 1 rule and a budget.")).toBeInTheDocument();
    expect(screen.getByText("Its 1 rule will be removed.")).toBeInTheDocument();
    expect(screen.getByText("Its budget will be removed.")).toBeInTheDocument();
    expect(remove).not.toHaveBeenCalled();

    const picker = screen.getByLabelText("Move its transactions to");
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "take" } });
    fireEvent.keyDown(picker, { key: "Enter" });
    expect(screen.getByText("Its 1 rule will move with them.")).toBeInTheDocument();

    fireEvent.click(button("Remove Coffee"));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(coffee.id, takeaway.id));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Removed Coffee. 2 transactions moved to Takeaway.",
    );
  });

  it("uncategorises them when nowhere is chosen", async () => {
    vi.spyOn(api, "categoryUsage").mockResolvedValue(USAGE);
    const remove = vi
      .spyOn(api, "removeCategory")
      .mockResolvedValue({ moved_transactions: 2, moved_rules: 0, removed_rules: 1, removed_budgets: 1 });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Coffee" }));
    await screen.findByText(/Coffee is used by/);
    fireEvent.click(button("Remove Coffee"));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(coffee.id, null));
    expect(await screen.findByRole("status")).toHaveTextContent("2 transactions moved to Uncategorised.");
  });

  it("does not offer the category being removed as where to move its transactions", async () => {
    vi.spyOn(api, "categoryUsage").mockResolvedValue(USAGE);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Coffee" }));
    const picker = await screen.findByLabelText("Move its transactions to");
    fireEvent.click(picker);

    const options = within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent);
    expect(options).toContain("Takeaway");
    expect(options).not.toContain("Coffee");
  });

  it("asks nothing more when nothing uses the category", async () => {
    vi.spyOn(api, "categoryUsage").mockResolvedValue({ transactions: 0, rules: 0, budgets: 0, subcategories: 0 });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Fuel" }));

    expect(await screen.findByText("Nothing uses Fuel, so removing it changes nothing else.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Move its transactions to")).not.toBeInTheDocument();
  });

  it("can be cancelled", async () => {
    vi.spyOn(api, "categoryUsage").mockResolvedValue(USAGE);
    const remove = vi.spyOn(api, "removeCategory");
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Coffee" }));
    await screen.findByText(/Coffee is used by/);
    fireEvent.click(button("Cancel"));

    expect(screen.queryByText(/Coffee is used by/)).not.toBeInTheDocument();
    expect(remove).not.toHaveBeenCalled();
  });

  it("shows why the app refused", async () => {
    vi.spyOn(api, "categoryUsage").mockResolvedValue(USAGE);
    vi.spyOn(api, "removeCategory").mockRejectedValue(new Error("Coffee still has categories in it."));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Coffee" }));
    await screen.findByText(/Coffee is used by/);
    fireEvent.click(button("Remove Coffee"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Coffee still has categories in it.");
  });

  it("is not offered for the two categories the app files into itself", async () => {
    show();
    await screen.findByText("Internal transfers");

    expect(screen.queryByRole("button", { name: "Remove Internal transfers" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove the Uncategorised group" })).not.toBeInTheDocument();
    expect(screen.getAllByText(/used by the app/)).toHaveLength(2);
  });

  it("is not offered for a group that still has categories in it, and is for an empty one", async () => {
    show();
    await screen.findByText("Coffee");

    expect(screen.queryByRole("button", { name: "Remove the Food & Drink (out) group" })).not.toBeInTheDocument();
    expect(button("Remove the Boat group")).toBeInTheDocument();
  });
});

describe("adding", () => {
  it("adds a category to the group it was asked for", async () => {
    const create = vi.spyOn(api, "createCategory").mockResolvedValue(cat("Bakery", food));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add a category to Food & Drink (out)" }));
    fireEvent.change(screen.getByLabelText("New category in Food & Drink (out)"), { target: { value: "Bakery" } });
    fireEvent.click(button("Add"));

    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: "Bakery", parent_id: food.id }));
    expect(await screen.findByRole("status")).toHaveTextContent("Added Bakery to Food & Drink (out).");
  });

  it("adds a group of the kind chosen", async () => {
    const create = vi.spyOn(api, "createCategory").mockResolvedValue(cat("Side income", null, { kind: "income" }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add a group" }));
    fireEvent.change(screen.getByLabelText("Group name"), { target: { value: "Side income" } });
    fireEvent.change(screen.getByLabelText("What it holds"), { target: { value: "income" } });
    fireEvent.click(button("Add group"));

    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: "Side income", kind: "income" }));
  });
});

describe("a read-only member", () => {
  it("sees the categories and no way to change them", async () => {
    role.value = "viewer";
    show();
    await screen.findByText("Coffee");

    expect(screen.getByText(/Only adults and owners can change them/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Rename|^Remove|Add/ })).not.toBeInTheDocument();
  });
});
