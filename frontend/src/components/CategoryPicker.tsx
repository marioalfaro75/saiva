import {
  type CSSProperties,
  type KeyboardEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import type { Category } from "../api/types";
import { readRecentCategories, rememberCategory } from "./categoryRecents";

/**
 * A category field you can type into.
 *
 * It replaces nine native <select>s that each listed all 78 subcategories in one flat
 * run, with the group a category belongs to nowhere on screen: "Supermarkets", with no
 * hint that it lives under Groceries. Three things make it quicker to find one:
 *
 *   - typing filters on the category AND its group, anywhere in the word, so "groc"
 *     finds both groceries subcategories and "fuel" finds Transport › Fuel;
 *   - the open list is grouped under its group headings, so browsing works too;
 *   - the last few categories picked sit at the top before anything is typed.
 *
 * It follows the ARIA combobox pattern: focus never leaves the input, and the highlighted
 * option is announced through aria-activedescendant.
 *
 * The list is rendered into document.body rather than beside the input. The transactions
 * table sits in a `.table-wrap` whose overflow-x: auto also clips vertically, so a list
 * positioned inside a row would be cut off at the table's edge. useDismissable treats
 * anything inside `[data-floating-layer]` as inside its dialog, so choosing a category in
 * the categorise dialog does not count as a click outside it.
 */

const LIST_MAX_HEIGHT = 320;

export interface CategoryPickerProps {
  /** Every category the household has, groups included: the groups give the headings. */
  categories: Category[];
  /** Which may be chosen. Defaults to subcategories, which is what most callers offer. */
  selectable?: (c: Category) => boolean;
  /** The chosen category id, or "" for none. */
  value: string;
  onChange: (id: string) => void;
  /** A real "no category" choice at the top of the list: "Uncategorised", "All categories". */
  emptyLabel?: string;
  /** What the field says while nothing is chosen, when that should differ from emptyLabel. */
  placeholder?: string;
  id?: string;
  "aria-label"?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

interface Opt {
  /** Unique within the open list: a category can appear under Recent and under its group. */
  key: string;
  id: string;
  label: string;
  /** "Group › Name", which is what the filter searches and the tooltip shows. */
  path: string;
}

interface Section {
  key: string;
  heading: string | null;
  options: Opt[];
}

const isSubcategory = (c: Category) => c.parent_id !== null;

/** Lower-cased with accents removed, so "cafe" finds "Café". */
function fold(text: string): string {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

/**
 * Where the floating list goes: under the input, or over it when the input is near the
 * bottom of the screen and there is more room above. Never wider than the screen.
 */
function placeUnder(el: HTMLElement): CSSProperties {
  const r = el.getBoundingClientRect();
  const below = window.innerHeight - r.bottom - 8;
  const above = r.top - 8;
  const upward = below < 200 && above > below;
  const maxHeight = Math.max(120, Math.min(LIST_MAX_HEIGHT, upward ? above : below));
  const width = Math.min(Math.max(r.width, 260), window.innerWidth - 16);
  const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
  return upward
    ? { left, width, maxHeight, bottom: window.innerHeight - r.top + 4 }
    : { left, width, maxHeight, top: r.bottom + 4 };
}

/** The groups as headings, each with its pickable categories, in the app's own order. */
function buildSections(
  categories: Category[],
  selectable: (c: Category) => boolean,
): { sections: Section[]; byId: Map<string, Opt> } {
  const known = new Map(categories.map((c) => [c.id, c]));
  const ordered = (a: Category, b: Category) => a.sort - b.sort || a.name.localeCompare(b.name);
  const sections: Section[] = [];
  const byId = new Map<string, Opt>();
  const add = (section: Section, c: Category, label: string, path: string) => {
    const opt = { key: `cat:${c.id}`, id: c.id, label, path };
    section.options.push(opt);
    byId.set(c.id, opt);
  };

  const groups = categories.filter((c) => c.parent_id === null).sort(ordered);
  for (const group of groups) {
    const section: Section = { key: `group:${group.id}`, heading: group.name, options: [] };
    // A group can itself be chosen where a caller allows it -- a budget on the whole group.
    if (selectable(group)) add(section, group, `${group.name} (whole group)`, group.name);
    const children = categories.filter((c) => c.parent_id === group.id && selectable(c));
    for (const child of children.sort(ordered)) {
      add(section, child, child.name, `${group.name} › ${child.name}`);
    }
    if (section.options.length > 0) sections.push(section);
  }

  // A category whose group is missing is still offered rather than silently dropped.
  const orphans = categories
    .filter((c) => c.parent_id !== null && !known.has(c.parent_id) && selectable(c))
    .sort(ordered);
  if (orphans.length > 0) {
    const section: Section = { key: "group:other", heading: "Other", options: [] };
    for (const c of orphans) add(section, c, c.name, c.name);
    sections.push(section);
  }
  return { sections, byId };
}

export function CategoryPicker({
  categories,
  selectable = isSubcategory,
  value,
  onChange,
  emptyLabel,
  placeholder,
  id,
  "aria-label": ariaLabel,
  className,
  disabled,
  required,
}: CategoryPickerProps) {
  const baseId = useId();
  const listId = `${baseId}-list`;
  const inputRef = useRef<HTMLInputElement>(null);
  const [focused, setFocused] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [recent, setRecent] = useState<string[]>([]);
  const [place, setPlace] = useState<CSSProperties | null>(null);

  const { sections, byId } = useMemo(
    () => buildSections(categories, selectable),
    [categories, selectable],
  );

  const chosen = value ? byId.get(value) : undefined;
  const chosenCategory = value ? categories.find((c) => c.id === value) : undefined;
  // What the field says when closed. A chosen id the list does not offer (a category the
  // caller filtered out, or one deleted since) still shows its name rather than nothing.
  const display = value
    ? (chosen?.label.replace(/ \(whole group\)$/, "") ?? chosenCategory?.name ?? "")
    : (placeholder ?? emptyLabel ?? "");

  // Everything the open list shows, in order, and the flat run of options the arrow keys
  // walk -- headings are not stops.
  const { shown, flat } = useMemo(() => {
    // A closed picker shows nothing, and the transactions table has one on every row.
    if (!open) return { shown: [] as Section[], flat: [] as Opt[] };
    const words = fold(query).split(/\s+/).filter(Boolean);
    const hits = (text: string) => words.every((w) => fold(text).includes(w));
    const out: Section[] = [];
    if (emptyLabel !== undefined && hits(emptyLabel)) {
      out.push({
        key: "empty",
        heading: null,
        options: [{ key: "empty", id: "", label: emptyLabel, path: emptyLabel }],
      });
    }
    if (words.length === 0) {
      const recentOpts = recent
        .map((rid) => byId.get(rid))
        .filter((o): o is Opt => o !== undefined)
        .map((o) => ({ ...o, key: `recent:${o.id}` }));
      if (recentOpts.length > 0) out.push({ key: "recent", heading: "Recent", options: recentOpts });
    }
    for (const section of sections) {
      const options = words.length === 0 ? section.options : section.options.filter((o) => hits(o.path));
      if (options.length > 0) out.push({ ...section, options });
    }
    return { shown: out, flat: out.flatMap((s) => s.options) };
  }, [open, query, emptyLabel, recent, byId, sections]);

  const optionDomId = (index: number) => `${baseId}-opt-${index}`;
  const activeIndex = flat.length === 0 ? -1 : Math.min(active, flat.length - 1);

  const showList = () => {
    if (inputRef.current) setPlace(placeUnder(inputRef.current));
    setOpen(true);
  };

  const openList = () => {
    if (disabled) return;
    const fresh = readRecentCategories();
    setRecent(fresh);
    setQuery("");
    showList();
    // Start on the current choice in its group, not on its copy under Recent. Counted
    // against the list as it is about to be, not the one still on screen: empty option,
    // then the recents that are still offered, then the groups in order.
    let at = 0;
    if (emptyLabel !== undefined) {
      if (value === "") {
        setActive(0);
        return;
      }
      at += 1;
    }
    at += fresh.filter((rid) => byId.has(rid)).length;
    for (const section of sections) {
      for (const opt of section.options) {
        if (opt.id === value) {
          setActive(at);
          return;
        }
        at += 1;
      }
    }
    setActive(0);
  };

  const close = () => {
    setOpen(false);
    setQuery("");
  };

  const choose = (opt: Opt) => {
    close();
    if (opt.id) rememberCategory(opt.id);
    // Like a native select: picking what is already chosen is not a change.
    if (opt.id !== value) onChange(opt.id);
  };

  // Leaving the field -- Tab, or a click anywhere else, such as on Apply -- after typing
  // keeps the match that was highlighted, as the ARIA pattern specifies and as a native
  // select's type-ahead did by changing its value as you typed. Without this, "type fuel,
  // click Apply" silently saved the OLD category: in the categorise dialog with "all from
  // this merchant" chosen, to every transaction from that merchant, and into a rule.
  // Opening the list and leaving without typing changes nothing; Escape backs out.
  const settle = () => {
    if (open && query.trim() !== "" && activeIndex >= 0) choose(flat[activeIndex]);
    else close();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // A key that finishes an IME composition (Japanese, Chinese, Korean, an Android
    // keyboard) belongs to the composition. Enter there confirms the word typed; acting
    // on it here would pick the first match -- and on the transactions table, save it.
    // Safari reports the confirming keydown after compositionend, as keyCode 229.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    switch (e.key) {
      case "ArrowDown":
      case "ArrowUp": {
        e.preventDefault();
        if (!open) {
          openList();
          return;
        }
        if (flat.length === 0) return;
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActive(Math.max(0, Math.min(flat.length - 1, activeIndex + step)));
        return;
      }
      case "Home":
      case "End":
        if (!open || flat.length === 0) return;
        e.preventDefault();
        setActive(e.key === "Home" ? 0 : flat.length - 1);
        return;
      case "Enter":
        // Never submits the surrounding form: on a closed picker it opens the list.
        e.preventDefault();
        if (!open) openList();
        else if (activeIndex >= 0) choose(flat[activeIndex]);
        return;
      case "Escape":
        if (!open) return;
        // Closes the list only. useDismissable leaves Escape on an expanded combobox
        // alone, so the dialog around it stays open.
        e.preventDefault();
        e.stopPropagation();
        close();
        return;
      case "Tab":
        // Not prevented: focus moves on as usual, with the highlighted match kept.
        if (open) settle();
        return;
    }
  };

  // Keep the floating list with its input while it is open. Checked every frame rather
  // than on scroll and resize alone: a notice disappearing above the field, or a row
  // refetched away, moves the input without either event, and the list was left behind
  // over the wrong row. One picker is open at a time, and nothing is set unless the
  // placement actually changed. Placed first when it opens, by showList.
  useEffect(() => {
    if (!open) return;
    let frame = 0;
    let last = "";
    const follow = () => {
      if (inputRef.current) {
        const next = placeUnder(inputRef.current);
        const key = JSON.stringify(next);
        if (key !== last) {
          last = key;
          setPlace(next);
        }
      }
      frame = window.requestAnimationFrame(follow);
    };
    frame = window.requestAnimationFrame(follow);
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  const activeDomId = open && activeIndex >= 0 ? optionDomId(activeIndex) : undefined;
  useLayoutEffect(() => {
    if (activeDomId) document.getElementById(activeDomId)?.scrollIntoView?.({ block: "nearest" });
  }, [activeDomId]);

  // Each option's place in the run the arrow keys walk. Keys are unique in it: a category
  // under Recent and under its group are "recent:<id>" and "cat:<id>".
  const position = new Map(flat.map((opt, i) => [opt.key, i]));
  const list = open ? (
    <div
      id={listId}
      role="listbox"
      aria-label={ariaLabel ?? "Categories"}
      className="catpick-list"
      data-floating-layer=""
      style={place ?? { visibility: "hidden" }}
      // Keep focus in the input, so a click chooses rather than blurring first.
      onMouseDown={(e) => e.preventDefault()}
    >
      {flat.length === 0 && (
        <div className="catpick-none" role="presentation">
          {query.trim() ? `No category matches “${query.trim()}”` : "No categories to choose from"}
        </div>
      )}
      {shown.map((section) => {
        const headingId = `${baseId}-${section.key}`;
        const options = section.options.map((opt) => {
          const i = position.get(opt.key) ?? -1;
          const classes = ["catpick-option"];
          if (i === activeIndex) classes.push("is-active");
          if (opt.id === value) classes.push("is-current");
          if (opt.id === "") classes.push("is-empty");
          return (
            <div
              key={opt.key}
              id={optionDomId(i)}
              role="option"
              aria-selected={opt.id === value}
              className={classes.join(" ")}
              title={opt.path}
              onMouseMove={() => {
                if (i !== activeIndex) setActive(i);
              }}
              onClick={() => choose(opt)}
            >
              {opt.label}
            </div>
          );
        });
        if (section.heading === null) return options;
        return (
          <div key={section.key} role="group" aria-labelledby={headingId}>
            <div id={headingId} role="presentation" className="catpick-heading">
              {section.heading}
            </div>
            {options}
          </div>
        );
      })}
    </div>
  ) : null;

  const inline = (className ?? "").split(/\s+/).includes("pill-select");
  return (
    <span className={inline ? "catpick catpick-inline" : "catpick"}>
      <input
        ref={inputRef}
        id={id}
        type="text"
        role="combobox"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-autocomplete="list"
        aria-activedescendant={activeDomId}
        autoComplete="off"
        spellCheck={false}
        className={className}
        disabled={disabled}
        required={required}
        // While focused the field is a search box; the current choice stays visible as
        // its placeholder. Blurred, it shows the choice itself.
        value={focused ? query : display}
        placeholder={focused ? display || placeholder : placeholder}
        title={chosen?.path ?? chosenCategory?.name}
        onFocus={() => {
          setFocused(true);
          setQuery("");
        }}
        onBlur={() => {
          setFocused(false);
          settle();
        }}
        onClick={() => {
          if (!open) openList();
        }}
        onChange={(e) => {
          if (!open) {
            setRecent(readRecentCategories());
            showList();
          }
          setQuery(e.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      <span className="catpick-chevron" aria-hidden="true">
        ▾
      </span>
      {list && createPortal(list, document.body)}
    </span>
  );
}
