import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { Category, Rule, RulePreview, RulePreviewRow } from "../api/types";
import { RulesManager } from "./RulesManager";

/**
 * Transactions → Rules. What has to hold: typing a pattern shows the transactions it
 * catches and what the rule would do to each; a rule made shows up under Your rules at
 * once and says what it moved; built-in rules are kept apart from the household's own;
 * deleting explains first and says what went back; and an edit shows as it is made,
 * going back -- with a word -- if the server refuses it.
 */

const cat = (id: string, name: string, parent: string | null = "g1"): Category => ({
  id,
  name,
  parent_id: parent,
  kind: "expense",
  icon: null,
  color: null,
  is_system: true,
  sort: 1,
});
const CATEGORIES = [
  cat("g1", "Food & Drink (out)", null),
  cat("coffee", "Coffee"),
  cat("takeaway", "Takeaway"),
];

const rule = (extra: Partial<Rule>): Rule => ({
  id: "r1",
  match_type: "contains",
  pattern: "woolworths",
  category_id: "takeaway",
  priority: 50,
  source: "system",
  is_active: true,
  ...extra,
});
const BUILT_IN = rule({ id: "sys1", pattern: "uber eats", source: "system" });
const MINE = rule({ id: "mine1", pattern: "campos", category_id: "coffee", source: "user", priority: 10 });

const row = (extra: Partial<RulePreviewRow>): RulePreviewRow => ({
  id: "t1",
  txn_date: "2026-11-03",
  description: "Campos Coffee",
  amount_cents: -698,
  category_id: null,
  category_name: null,
  outcome: "change",
  other_rule_pattern: null,
  other_rule_category_name: null,
  ...extra,
});
const preview = (rows: RulePreviewRow[], extra: Partial<RulePreview> = {}): RulePreview => ({
  matched: rows.length,
  fillable: 0,
  samples: [],
  will_change: rows.filter((r) => r.outcome === "change").length,
  already: rows.filter((r) => r.outcome === "same").length,
  locked: rows.filter((r) => r.outcome === "locked").length,
  kept: rows.filter((r) => r.outcome === "kept").length,
  other_rule: rows.filter((r) => r.outcome === "other_rule").length,
  rows,
  ...extra,
});

const flash = vi.fn();
function show() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RulesManager categories={CATEGORIES} onFlash={flash} />
    </QueryClientProvider>,
  );
}

const matchesField = () => screen.getByRole("textbox", { name: "Matches" });
const typePattern = (text: string) => fireEvent.change(matchesField(), { target: { value: text } });
const chooseCategory = (name: string) => {
  const picker = screen.getByRole("combobox", { name: "Category" });
  fireEvent.focus(picker);
  fireEvent.click(picker);
  fireEvent.change(picker, { target: { value: name } });
  fireEvent.keyDown(picker, { key: "Enter" });
};
const status = () => screen.getAllByRole("status")[0];
const yourRules = () => screen.getByRole("heading", { name: "Your rules" }).closest(".card") as HTMLElement;

beforeEach(() => {
  vi.restoreAllMocks();
  flash.mockReset();
  window.localStorage.clear();
});

describe("the preview", () => {
  it("lists the transactions a pattern catches, and what the rule would do to each", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([BUILT_IN]);
    const previewRule = vi.spyOn(api, "previewRule").mockResolvedValue(
      preview([
        row({ id: "t1", description: "Campos Coffee" }),
        row({ id: "t2", description: "The Coffee Club", amount_cents: -530 }),
      ]),
    );
    show();

    typePattern("c");
    typePattern("co");
    typePattern("coffee");

    const list = await screen.findByRole("region", { name: "Transactions this rule catches" });
    const rows = within(list).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1].textContent)).toEqual([
      "Campos Coffee",
      "The Coffee Club",
    ]);
    expect(rows[1]).toHaveTextContent("-$5.30");
    expect(rows[0]).toHaveTextContent("Uncategorised");
    expect(status()).toHaveTextContent(
      "Catches 2 transactions: 2 will be filed once you choose a category.",
    );
    // Asked once the typing settled, not at every keystroke.
    expect(previewRule).toHaveBeenCalledTimes(1);
    expect(previewRule).toHaveBeenCalledWith({
      match_type: "contains",
      pattern: "coffee",
      category_id: undefined,
    });
  });

  it("asks again with the category once one is chosen, and names it", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([]);
    const previewRule = vi
      .spyOn(api, "previewRule")
      .mockResolvedValue(preview([row({ outcome: "change" })]));
    show();
    typePattern("coffee");
    await screen.findByRole("region", { name: "Transactions this rule catches" });

    chooseCategory("Coffee");

    await waitFor(() =>
      expect(previewRule).toHaveBeenLastCalledWith({
        match_type: "contains",
        pattern: "coffee",
        category_id: "coffee",
      }),
    );
    await waitFor(() =>
      expect(status()).toHaveTextContent("Catches 1 transaction: 1 will move to Coffee."),
    );
    expect(screen.getByText("→ Coffee")).toBeInTheDocument();
  });

  it("says why a transaction would be left alone", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([]);
    vi.spyOn(api, "previewRule").mockResolvedValue(
      preview([
        row({ id: "a", outcome: "kept", category_name: "Takeaway" }),
        row({ id: "b", outcome: "locked" }),
        row({
          id: "c",
          outcome: "other_rule",
          other_rule_pattern: "campos",
          other_rule_category_name: "Takeaway",
        }),
      ]),
    );
    show();
    typePattern("coffee");

    await screen.findByText("Kept: your choice");
    expect(screen.getByText("Kept: locked")).toBeInTheDocument();
    expect(screen.getByText("Kept: rule “campos” (Takeaway) comes first")).toBeInTheDocument();
    expect(status()).toHaveTextContent(
      "Catches 3 transactions: 1 kept: you chose their category, 1 kept: locked, " +
        "1 taken first by another rule.",
    );
  });

  it("says when nothing matches, and that the rule still files future imports", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([]);
    vi.spyOn(api, "previewRule").mockResolvedValue(preview([]));
    show();
    typePattern("zzz");
    await waitFor(() =>
      expect(status()).toHaveTextContent(
        "No transactions match yet. The rule will still file future imports that do.",
      ),
    );
    expect(screen.queryByRole("region", { name: "Transactions this rule catches" })).toBeNull();
  });

  it("dims the last answer while the next is on its way, instead of passing it off", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([]);
    vi.spyOn(api, "previewRule")
      .mockResolvedValueOnce(preview([row({ description: "Campos Coffee" })]))
      .mockReturnValue(new Promise(() => {}));
    show();
    typePattern("campos");
    const list = await screen.findByRole("region", { name: "Transactions this rule catches" });
    expect(list.parentElement).not.toHaveClass("is-stale");

    typePattern("campos coffee");

    expect(status()).toHaveTextContent("Checking…");
    await waitFor(() => expect(list.parentElement).toHaveClass("is-stale"));
  });

  it("says if the pattern couldn't be checked", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([]);
    vi.spyOn(api, "previewRule").mockRejectedValue(new Error("boom"));
    show();
    typePattern("coffee");
    await waitFor(() => expect(status()).toHaveTextContent("Couldn't check that pattern."));
  });
});

describe("making a rule", () => {
  it("shows it under Your rules at once, picked out, and says what it moved", async () => {
    // The list refetch after saving never answers here, so the row can only come from
    // the save's own answer.
    vi.spyOn(api, "rules").mockResolvedValueOnce([BUILT_IN]).mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "previewRule").mockResolvedValue(preview([row({})]));
    const createRule = vi.spyOn(api, "createRule").mockResolvedValue({
      ...rule({ id: "new1", pattern: "coffee", category_id: "coffee", source: "user", priority: 10 }),
      categorised: 9,
      uncategorised: 0,
    });
    show();
    await screen.findByText("No rules of your own yet. Make one above.");
    typePattern("coffee");
    chooseCategory("Coffee");

    fireEvent.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith("Rule created. 9 transactions moved to Coffee."),
    );
    expect(createRule).toHaveBeenCalledWith({
      match_type: "contains",
      pattern: "coffee",
      category_id: "coffee",
    });
    const added = within(yourRules()).getByRole("textbox", { name: "Text the rule “coffee” matches" });
    expect(added.closest("tr")).toHaveClass("rule-new");
    expect(matchesField()).toHaveValue("");
  });

  it("says so when a new rule had nothing to change", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([]);
    vi.spyOn(api, "previewRule").mockResolvedValue(preview([]));
    vi.spyOn(api, "createRule").mockResolvedValue({
      ...rule({ id: "new1", pattern: "zzz", source: "user" }),
      categorised: 0,
      uncategorised: 0,
    });
    show();
    typePattern("zzz");
    chooseCategory("Takeaway");
    fireEvent.click(screen.getByRole("button", { name: "Create rule" }));
    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith("Rule created. No transactions needed changing."),
    );
  });
});

describe("the lists", () => {
  it("keeps the household's rules apart from the built-in ones", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([BUILT_IN, MINE]);
    show();

    await within(yourRules()).findByRole("textbox", { name: "Text the rule “campos” matches" });
    expect(within(yourRules()).queryByRole("textbox", { name: /uber eats/ })).toBeNull();
    const builtIn = screen.getByText("Built-in rules").closest("details") as HTMLElement;
    expect(builtIn).toHaveTextContent("· 1");
    expect(
      within(builtIn).getByRole("textbox", { name: "Text the rule “uber eats” matches", hidden: true }),
    ).toBeInTheDocument();
  });

  it("shows a switch-off as it is made", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([MINE]);
    vi.spyOn(api, "updateRule").mockReturnValue(new Promise(() => {}));
    show();
    const box = await screen.findByRole("checkbox", { name: "Rule “campos” is active" });

    fireEvent.click(box);

    expect(box).not.toBeChecked();
  });

  it("puts a refused edit back, and says it wasn't saved", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([MINE]);
    vi.spyOn(api, "updateRule").mockRejectedValue(new Error("nope"));
    show();
    const box = await screen.findByRole("checkbox", { name: "Rule “campos” is active" });

    fireEvent.click(box);

    await waitFor(() => expect(flash).toHaveBeenCalledWith("That change to the rule wasn't saved."));
    expect(box).toBeChecked();
  });

  it("says what an edit moved", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([MINE]);
    vi.spyOn(api, "updateRule").mockResolvedValue({
      ...MINE,
      is_active: false,
      categorised: 0,
      uncategorised: 4,
    });
    show();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Rule “campos” is active" }));
    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith("Rule updated. 4 transactions back to Uncategorised."),
    );
  });

  it("explains before deleting, then says what went back", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([MINE]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const deleteRule = vi
      .spyOn(api, "deleteRule")
      .mockResolvedValue({ categorised: 2, uncategorised: 7 });
    show();
    const row = (await within(yourRules()).findByRole("textbox", { name: /campos/ })).closest(
      "tr",
    ) as HTMLElement;

    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    expect(confirm).toHaveBeenCalledWith(
      "Delete the rule “campos”? Transactions it filed go back to Uncategorised, " +
        "unless another rule catches them.",
    );
    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith(
        "Rule deleted. 2 transactions now filed by another rule, 7 transactions back to Uncategorised.",
      ),
    );
    expect(deleteRule).toHaveBeenCalledWith("mine1");
  });

  it("deletes nothing if the person backs out", async () => {
    vi.spyOn(api, "rules").mockResolvedValue([MINE]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const deleteRule = vi.spyOn(api, "deleteRule");
    show();
    const row = (await within(yourRules()).findByRole("textbox", { name: /campos/ })).closest(
      "tr",
    ) as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    expect(deleteRule).not.toHaveBeenCalled();
  });
});
