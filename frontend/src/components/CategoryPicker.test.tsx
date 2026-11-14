import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Category } from "../api/types";
import { useDismissable } from "../hooks/useDismissable";
import { CategoryPicker } from "./CategoryPicker";
import { readRecentCategories } from "./categoryRecents";

/**
 * The category field you can type into.
 *
 * It replaced nine selects that each listed 78 subcategories flat, with no group in
 * sight. What has to hold: typing finds a category by its own name OR its group's;
 * results stay under their group; recents come first; the keyboard works without the
 * mouse; and inside a dialog, neither Escape nor a click in the floating list closes
 * the dialog around it.
 */

let n = 0;
function cat(name: string, parent: Category | null, extra: Partial<Category> = {}): Category {
  n += 1;
  return {
    id: `c${n}`,
    name,
    parent_id: parent?.id ?? null,
    kind: "expense",
    icon: null,
    color: null,
    is_system: true,
    sort: n,
    ...extra,
  };
}

const groceries = cat("Groceries", null);
const supermarkets = cat("Supermarkets", groceries);
const butcher = cat("Butcher/Greengrocer", groceries);
const transport = cat("Transport", null);
const fuel = cat("Fuel", transport);
const parking = cat("Parking", transport);
const food = cat("Eating out", null);
const cafe = cat("Café", food);
const ALL = [groceries, supermarkets, butcher, transport, fuel, parking, food, cafe];

function Harness(props: {
  initial?: string;
  emptyLabel?: string;
  placeholder?: string;
  selectable?: (c: Category) => boolean;
  onChange?: (id: string) => void;
}) {
  const [value, setValue] = useState(props.initial ?? "");
  return (
    <CategoryPicker
      categories={ALL}
      value={value}
      onChange={(id) => {
        setValue(id);
        props.onChange?.(id);
      }}
      emptyLabel={props.emptyLabel}
      placeholder={props.placeholder}
      selectable={props.selectable}
      aria-label="Category"
    />
  );
}

const field = () => screen.getByRole("combobox", { name: "Category" });
const options = () => screen.queryAllByRole("option").map((o) => o.textContent);
const type = (text: string) => fireEvent.change(field(), { target: { value: text } });
const key = (k: string) => fireEvent.keyDown(field(), { key: k });

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("finding a category by typing", () => {
  it("matches on the category's own name", () => {
    render(<Harness />);
    fireEvent.focus(field());
    type("fuel");

    expect(options()).toEqual(["Fuel"]);
  });

  it("matches on the group, so a category is found by what it belongs to", () => {
    // The flat list's whole failure: "Supermarkets" gave no hint it was under Groceries.
    render(<Harness />);
    fireEvent.focus(field());
    type("groc");

    expect(options()).toEqual(["Supermarkets", "Butcher/Greengrocer"]);
  });

  it("matches anywhere in the word, and on every word typed", () => {
    render(<Harness />);
    fireEvent.focus(field());
    type("trans park");

    expect(options()).toEqual(["Parking"]);
  });

  it("ignores accents and case", () => {
    render(<Harness />);
    fireEvent.focus(field());
    type("CAFE");

    expect(options()).toEqual(["Café"]);
  });

  it("says so when nothing matches, rather than showing an empty box", () => {
    render(<Harness />);
    fireEvent.focus(field());
    type("zzz");

    expect(options()).toEqual([]);
    expect(screen.getByText(/no category matches “zzz”/i)).toBeInTheDocument();
  });
});

describe("the open list", () => {
  it("groups categories under their group, by name", () => {
    render(<Harness />);
    fireEvent.click(field());

    const group = screen.getByRole("group", { name: "Transport" });
    expect(within(group).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Fuel",
      "Parking",
    ]);
  });

  it("offers a group itself only where the caller allows it, and says it means the whole group", () => {
    render(<Harness selectable={(c) => c.kind === "expense"} />);
    fireEvent.click(field());

    const group = screen.getByRole("group", { name: "Groceries" });
    expect(within(group).getAllByRole("option")[0]).toHaveTextContent("Groceries (whole group)");
  });

  it("puts the empty choice first when there is one, and choosing it clears", () => {
    const onChange = vi.fn();
    render(<Harness initial={fuel.id} emptyLabel="Uncategorised" onChange={onChange} />);
    fireEvent.click(field());

    expect(options()[0]).toBe("Uncategorised");
    fireEvent.click(screen.getByRole("option", { name: "Uncategorised" }));
    expect(onChange).toHaveBeenCalledWith("");
  });
});

describe("what the field shows", () => {
  it("shows the current category's name when not in use, with its group as the tooltip", () => {
    render(<Harness initial={fuel.id} />);

    expect(field()).toHaveValue("Fuel");
    expect(field()).toHaveAttribute("title", "Transport › Fuel");
  });

  it("shows the placeholder while nothing is chosen", () => {
    render(<Harness placeholder="Choose a category…" />);

    expect(field()).toHaveValue("Choose a category…");
  });

  it("becomes an empty search box on focus, keeping the choice visible as a hint", () => {
    render(<Harness initial={fuel.id} />);
    fireEvent.focus(field());

    expect(field()).toHaveValue("");
    expect(field()).toHaveAttribute("placeholder", "Fuel");
  });
});

describe("the keyboard", () => {
  it("opens with the arrow key, moves, and chooses with Enter", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.focus(field());

    key("ArrowDown");
    expect(field()).toHaveAttribute("aria-expanded", "true");
    key("ArrowDown");
    key("Enter");

    expect(onChange).toHaveBeenCalledWith(butcher.id);
    expect(field()).toHaveAttribute("aria-expanded", "false");
  });

  it("announces the highlighted option to a screen reader", () => {
    render(<Harness />);
    fireEvent.focus(field());
    key("ArrowDown");

    const active = field().getAttribute("aria-activedescendant");
    expect(active).toBeTruthy();
    expect(document.getElementById(active!)).toHaveTextContent("Supermarkets");
  });

  it("starts on the current choice when it opens", () => {
    render(<Harness initial={parking.id} />);
    fireEvent.focus(field());
    key("ArrowDown");

    const active = field().getAttribute("aria-activedescendant");
    expect(document.getElementById(active!)).toHaveTextContent("Parking");
  });

  it("closes with Escape and changes nothing", () => {
    const onChange = vi.fn();
    render(<Harness initial={fuel.id} onChange={onChange} />);
    fireEvent.focus(field());
    type("park");
    key("Escape");

    expect(field()).toHaveAttribute("aria-expanded", "false");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.blur(field());
    expect(field()).toHaveValue("Fuel");
  });

  it("never lets Enter reach the form it sits in, open or closed", () => {
    // jsdom never submits a form on a synthetic keydown, so asserting onSubmit was not
    // called proved nothing. What stops a real browser submitting is preventDefault on
    // the keydown, and fireEvent reports that by returning false.
    render(<Harness />);
    fireEvent.focus(field());

    expect(fireEvent.keyDown(field(), { key: "Enter" })).toBe(false);
    expect(field()).toHaveAttribute("aria-expanded", "true");
    expect(fireEvent.keyDown(field(), { key: "Enter" })).toBe(false);
  });

  it("leaves a key that finishes an IME composition to the composition", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.focus(field());
    type("groc");

    // Enter confirming a composed word, as Chrome/Firefox and as Safari report it.
    fireEvent.keyDown(field(), { key: "Enter", isComposing: true });
    fireEvent.keyDown(field(), { key: "Enter", keyCode: 229 });

    expect(onChange).not.toHaveBeenCalled();
    expect(field()).toHaveAttribute("aria-expanded", "true");
  });

  it("does not report a change when the current choice is picked again", () => {
    const onChange = vi.fn();
    render(<Harness initial={fuel.id} onChange={onChange} />);
    fireEvent.focus(field());
    key("ArrowDown");
    key("Enter");

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("leaving the field", () => {
  // The review's blocking finding. A native select's type-ahead changed its value as you
  // typed, so "type, then Tab" or "type, then click Apply" worked. The first version of
  // this picker threw the typed match away on both, and Apply saved the OLD category.
  it("keeps the highlighted match when Tab moves on, and lets focus move", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.focus(field());
    type("fuel");

    expect(fireEvent.keyDown(field(), { key: "Tab" })).toBe(true);
    expect(onChange).toHaveBeenCalledWith(fuel.id);
  });

  it("keeps the highlighted match when a click elsewhere takes focus", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.focus(field());
    type("park");
    fireEvent.blur(field());

    expect(onChange).toHaveBeenCalledWith(parking.id);
    expect(field()).toHaveValue("Parking");
  });

  it("changes nothing when the list was only opened, not typed into", () => {
    const onChange = vi.fn();
    render(<Harness initial={fuel.id} onChange={onChange} />);
    fireEvent.click(field());
    key("ArrowDown");
    fireEvent.blur(field());

    expect(onChange).not.toHaveBeenCalled();
  });

  it("changes nothing when what was typed matches nothing", () => {
    const onChange = vi.fn();
    render(<Harness initial={fuel.id} onChange={onChange} />);
    fireEvent.focus(field());
    type("zzz");
    fireEvent.blur(field());

    expect(onChange).not.toHaveBeenCalled();
    expect(field()).toHaveValue("Fuel");
  });

  it("changes nothing after Escape, which is how to back out", () => {
    const onChange = vi.fn();
    render(<Harness initial={fuel.id} onChange={onChange} />);
    fireEvent.focus(field());
    type("park");
    key("Escape");
    fireEvent.blur(field());

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("an empty list", () => {
  it("says what was not found when something was typed", () => {
    render(<Harness />);
    fireEvent.focus(field());
    type("zzz");

    expect(screen.getByText("No category matches “zzz”")).toBeInTheDocument();
  });

  it("does not claim a search failed when nothing was typed", () => {
    render(<Harness selectable={() => false} />);
    fireEvent.click(field());

    expect(screen.getByText("No categories to choose from")).toBeInTheDocument();
    expect(screen.queryByText(/no category matches/i)).not.toBeInTheDocument();
  });
});

describe("staying with its field", () => {
  it("moves with the field when the page shifts without scrolling", async () => {
    // A notice disappearing above the field moves it with no scroll or resize event.
    let top = 300;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({ top, bottom: top + 30, left: 40, right: 240, width: 200, height: 30, x: 40, y: top, toJSON: () => ({}) }) as DOMRect,
    );
    const frame = () => act(() => new Promise((r) => window.requestAnimationFrame(() => r(null))));
    render(<Harness />);
    fireEvent.click(field());
    await frame();
    expect(screen.getByRole("listbox").style.top).toBe("334px");

    top = 246;
    await frame();
    await frame();
    expect(screen.getByRole("listbox").style.top).toBe("280px");
  });
});

describe("recently used", () => {
  it("puts the last categories picked at the top, newest first", () => {
    render(<Harness />);
    for (const name of ["Parking", "Café"]) {
      fireEvent.click(field());
      type(name.slice(0, 3));
      fireEvent.click(screen.getByRole("option", { name }));
    }
    fireEvent.click(field());

    const recent = screen.getByRole("group", { name: "Recent" });
    expect(within(recent).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Café",
      "Parking",
    ]);
    expect(readRecentCategories()).toEqual([cafe.id, parking.id]);
  });

  it("is not shown once you start typing", () => {
    window.localStorage.setItem("saiva.recentCategories", JSON.stringify([fuel.id]));
    render(<Harness />);
    fireEvent.focus(field());
    type("f");

    expect(screen.queryByRole("group", { name: "Recent" })).not.toBeInTheDocument();
  });

  it("skips a remembered category this field does not offer", () => {
    window.localStorage.setItem("saiva.recentCategories", JSON.stringify(["gone", fuel.id]));
    render(<Harness />);
    fireEvent.click(field());

    const recent = screen.getByRole("group", { name: "Recent" });
    expect(within(recent).getAllByRole("option").map((o) => o.textContent)).toEqual(["Fuel"]);
  });

  it("still works when the browser refuses storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.click(field());
    fireEvent.click(screen.getByRole("option", { name: "Fuel" }));

    expect(onChange).toHaveBeenCalledWith(fuel.id);
  });
});

describe("inside a dialog", () => {
  // The dialog listens in the CAPTURE phase on document: it sees Escape before the
  // picker does, and it judged a click in the floating list -- which lives in
  // document.body, outside the dialog -- as a click outside. Both closed the dialog.
  function Dialog({ onClose }: { onClose: () => void }) {
    const panel = useDismissable<HTMLDivElement>(true, onClose);
    return (
      <div ref={panel} role="dialog" aria-label="Categorise">
        <Harness />
      </div>
    );
  }

  it("Escape closes the picker's list, not the dialog; a second Escape closes the dialog", () => {
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} />);
    fireEvent.click(field());
    expect(field()).toHaveAttribute("aria-expanded", "true");

    fireEvent.keyDown(field(), { key: "Escape" });
    expect(field()).toHaveAttribute("aria-expanded", "false");
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(field(), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("choosing in the floating list does not count as a click outside the dialog", () => {
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} />);
    fireEvent.click(field());
    const option = screen.getByRole("option", { name: "Parking" });

    fireEvent.pointerDown(option);
    fireEvent.pointerUp(option);
    fireEvent.click(option);

    expect(onClose).not.toHaveBeenCalled();
    // The dialog put focus in the field when it opened, and a focused field is a search
    // box with the choice as its hint -- the value itself shows once focus moves on.
    expect(field()).toHaveAttribute("placeholder", "Parking");
    fireEvent.blur(field());
    expect(field()).toHaveValue("Parking");
  });

  it("a press outside while the list is open closes the list, not the dialog", () => {
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} />);
    fireEvent.click(field());

    fireEvent.pointerDown(document.body);
    fireEvent.blur(field());
    fireEvent.pointerUp(document.body);
    expect(onClose).not.toHaveBeenCalled();
    expect(field()).toHaveAttribute("aria-expanded", "false");

    fireEvent.pointerDown(document.body);
    fireEvent.pointerUp(document.body);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("survives the page behind it re-rendering, with what was typed intact", () => {
    // Callers pass a fresh onClose arrow each render. The dialog's effect used to depend
    // on it, so every re-render sent focus out and back in: the list closed and the
    // typed letters were erased -- or, now that leaving keeps the match, chosen.
    //
    // The dialog is opened from a focused button, as "More actions" is: that button is
    // where the bounce sent focus. Without one, focus had nowhere to go and the bug hid.
    function Page() {
      const [tick, setTick] = useState(0);
      const [shown, setShown] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setShown(true)}>
            More actions
          </button>
          <button type="button" onClick={() => setTick(tick + 1)}>
            Refresh {tick}
          </button>
          {shown && <Dialog onClose={() => {}} />}
        </>
      );
    }
    render(<Page />);
    const opener = screen.getByRole("button", { name: "More actions" });
    opener.focus();
    fireEvent.click(opener);
    expect(field()).toHaveFocus();
    type("fu");
    // fireEvent.click does not move focus, so these re-render the page and nothing else.
    fireEvent.click(screen.getByRole("button", { name: /^Refresh/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Refresh/ }));

    expect(field()).toHaveAttribute("aria-expanded", "true");
    expect(field()).toHaveValue("fu");
  });

  it("a click genuinely outside still closes the dialog", () => {
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} />);

    fireEvent.pointerDown(document.body);
    fireEvent.pointerUp(document.body);

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
