import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type FormEvent,
  type KeyboardEvent,
  type RefObject,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { api } from "../api/client";
import type { Category, CategoryUsage } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { CategoryPicker } from "./CategoryPicker";

/**
 * Settings → Categories: add, rename and remove the categories transactions go in.
 *
 * Renaming is a label only -- the app finds the built-in categories it relies on by a
 * key that a rename leaves alone. Removing asks where the category's transactions should
 * go, says what else it touches before anything happens, and is not offered at all for
 * the two categories the app files into itself, or for a group that still has
 * categories in it. A read-only member sees the list and no buttons.
 */

const KINDS = [
  { value: "expense", label: "Spending" },
  { value: "income", label: "Income" },
  { value: "savings", label: "Savings" },
  { value: "transfer", label: "Transfers" },
];

const errorText = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong.");
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const ordered = (a: Category, b: Category) => a.sort - b.sort || a.name.localeCompare(b.name);
const fold = (s: string) =>
  s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();

interface Group {
  group: Category | null;
  heading: string;
  children: Category[];
}

export function CategoriesManager() {
  const { me } = useAuth();
  const canEdit = me !== null && me.user.role !== "viewer";
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const [filter, setFilter] = useState("");
  const [flash, setFlash] = useState<string | null>(null);
  // Which groups the person has opened. Kept here rather than in each group, because a
  // group the find box filters out is unmounted -- and came back closed when it cleared.
  const [openGroups, setOpenGroups] = useState<ReadonlySet<string>>(() => new Set());
  const setGroupOpen = (key: string, isOpen: boolean) =>
    setOpenGroups((prev) => {
      if (prev.has(key) === isOpen) return prev;
      const next = new Set(prev);
      if (isOpen) next.add(key);
      else next.delete(key);
      return next;
    });
  const fid = useId();

  const all = categories.data;
  const groups = useMemo<Group[]>(() => {
    const list = all ?? [];
    const known = new Set(list.map((c) => c.id));
    const out: Group[] = list
      .filter((c) => c.parent_id === null)
      .sort(ordered)
      .map((g) => ({
        group: g,
        heading: g.name,
        children: list.filter((c) => c.parent_id === g.id).sort(ordered),
      }));
    // A category whose group is missing is still listed, so it can still be removed.
    const orphans = list.filter((c) => c.parent_id !== null && !known.has(c.parent_id)).sort(ordered);
    if (orphans.length > 0) out.push({ group: null, heading: "Other", children: orphans });
    return out;
  }, [all]);

  const words = fold(filter).split(/\s+/).filter(Boolean);
  const matches = (text: string) => words.every((w) => fold(text).includes(w));
  const shown =
    words.length === 0
      ? groups
      : groups
          .map((g) =>
            matches(g.heading)
              ? g
              : { ...g, children: g.children.filter((c) => matches(`${g.heading} ${c.name}`)) },
          )
          .filter((g) => matches(g.heading) || g.children.length > 0);

  return (
    <div className="card cat-manager">
      <h2>Categories</h2>
      <p className="muted">
        The categories your transactions are sorted into.
        {canEdit
          ? " Rename them, add your own, or remove ones you don't use."
          : " Only adults and owners can change them."}
      </p>
      {flash && (
        <div className="notice notice-good" role="status">
          {flash}
        </div>
      )}

      <div className="field">
        <label htmlFor={`${fid}-find`}>Find a category</label>
        <input
          id={`${fid}-find`}
          type="search"
          value={filter}
          placeholder="Type to narrow the list"
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>

      {categories.isLoading && <p className="muted">Loading categories…</p>}
      {categories.isError && (
        <div className="notice notice-warn">Couldn&rsquo;t load the categories. {errorText(categories.error)}</div>
      )}
      {words.length > 0 && shown.length === 0 && (
        <p className="muted">No category matches &ldquo;{filter.trim()}&rdquo;.</p>
      )}

      <div className="cat-groups">
        {shown.map((g) => (
          <GroupBlock
            key={g.group?.id ?? "other"}
            entry={g}
            userOpen={openGroups.has(g.group?.id ?? "other")}
            onUserToggle={(isOpen) => setGroupOpen(g.group?.id ?? "other", isOpen)}
            all={all ?? []}
            canEdit={canEdit}
            forceOpen={words.length > 0}
            onDone={setFlash}
          />
        ))}
      </div>

      {canEdit && words.length === 0 && <AddGroup onDone={setFlash} />}
    </div>
  );
}

function GroupBlock({
  entry,
  all,
  canEdit,
  forceOpen,
  userOpen,
  onUserToggle,
  onDone,
}: {
  entry: Group;
  all: Category[];
  canEdit: boolean;
  forceOpen: boolean;
  userOpen: boolean;
  onUserToggle: (open: boolean) => void;
  onDone: (message: string) => void;
}) {
  const { group, heading, children } = entry;
  const kind = group && group.kind !== "expense" ? KINDS.find((k) => k.value === group.kind)?.label : null;
  const summary = (
    <>
      <span className="cat-group-name">{heading}</span>
      <span className="muted">
        {" · "}
        {plural(children.length, "category", "categories")}
        {kind ? ` · ${kind}` : ""}
      </span>
    </>
  );
  const body = (
    <div className="cat-group-body">
      {group && (
        <CategoryRow
          category={group}
          all={all}
          canEdit={canEdit}
          isGroup
          childCount={children.length}
          onDone={onDone}
        />
      )}
      {children.map((c) => (
        <CategoryRow key={c.id} category={c} all={all} canEdit={canEdit} onDone={onDone} />
      ))}
      {canEdit && group && <AddCategory group={group} onDone={onDone} />}
    </div>
  );
  // Each group is a disclosure, which keeps 20 groups and 78 categories to one screen;
  // while the find box has text, every group with a match is held open. Always the same
  // element: switching between a <details> and a <section> when the find box filled or
  // emptied remounted the whole group, discarding an open Rename or Remove and closing
  // every group the person had opened.
  return (
    <details
      className="cat-group"
      open={forceOpen || userOpen}
      onToggle={(e) => {
        // Only the person's own opening and closing is remembered, not the find box's.
        if (!forceOpen) onUserToggle(e.currentTarget.open);
      }}
    >
      <summary className="cat-group-summary">{summary}</summary>
      {body}
    </details>
  );
}

function CategoryRow({
  category,
  all,
  canEdit,
  isGroup = false,
  childCount = 0,
  onDone,
}: {
  category: Category;
  all: Category[];
  canEdit: boolean;
  isGroup?: boolean;
  childCount?: number;
  onDone: (message: string) => void;
}) {
  const [mode, setMode] = useState<"view" | "rename" | "remove">("view");
  const renameButton = useRef<HTMLButtonElement>(null);
  const removeButton = useRef<HTMLButtonElement>(null);
  // Which button opened the panel, so closing it puts the keyboard back there instead of
  // dropping focus to the top of the page (the button is not rendered while it is open).
  const openedBy = useRef<RefObject<HTMLButtonElement | null> | null>(null);
  useEffect(() => {
    if (mode !== "view" || !openedBy.current) return;
    openedBy.current.current?.focus();
    openedBy.current = null;
  }, [mode]);
  const open = (next: "rename" | "remove") => {
    openedBy.current = next === "rename" ? renameButton : removeButton;
    setMode(next);
  };
  const label = isGroup ? `the ${category.name} group` : category.name;
  // A group with categories in it, and the two the app files into itself, cannot go.
  const removable = !category.protected && !(isGroup && childCount > 0);

  return (
    <div className={isGroup ? "cat-row cat-row-group" : "cat-row"}>
      {mode === "rename" ? (
        <RenameForm category={category} onClose={() => setMode("view")} onDone={onDone} />
      ) : (
        <div className="cat-row-line">
          <span className="cat-row-name">
            {isGroup ? "Group name: " : ""}
            <strong>{category.name}</strong>
            {category.protected && (
              <span
                className="muted cat-row-note"
                title="The app files transactions into this one itself, so it can be renamed but not removed."
              >
                {" "}
                · used by the app
              </span>
            )}
          </span>
          {canEdit && mode === "view" && (
            <span className="cat-row-actions">
              <button
                ref={renameButton}
                type="button"
                className="btn btn-ghost"
                aria-label={`Rename ${label}`}
                onClick={() => open("rename")}
              >
                Rename
              </button>
              {removable && (
                <button
                  ref={removeButton}
                  type="button"
                  className="btn btn-ghost"
                  aria-label={`Remove ${label}`}
                  onClick={() => open("remove")}
                >
                  Remove
                </button>
              )}
            </span>
          )}
        </div>
      )}
      {mode === "remove" && (
        <RemovePanel category={category} all={all} onClose={() => setMode("view")} onDone={onDone} />
      )}
    </div>
  );
}

function RenameForm({
  category,
  onClose,
  onDone,
}: {
  category: Category;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(category.name);
  const rename = useMutation({
    mutationFn: (name: string) => api.renameCategory(category.id, name),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["categories"] });
      onDone(`Renamed ${category.name} to ${updated.name}.`);
      onClose();
    },
  });
  const unchanged = draft.trim() === category.name;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (unchanged) onClose();
    else if (draft.trim()) rename.mutate(draft);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };
  return (
    <form className="cat-row-form" onSubmit={submit}>
      <input
        aria-label={`New name for ${category.name}`}
        value={draft}
        maxLength={80}
        autoFocus
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <button type="submit" className="btn btn-primary" disabled={!draft.trim() || rename.isPending}>
        Save
      </button>
      <button type="button" className="btn btn-ghost" onClick={onClose}>
        Cancel
      </button>
      {rename.isError && (
        <div className="notice notice-warn cat-row-error" role="alert">
          {errorText(rename.error)}
        </div>
      )}
    </form>
  );
}

function usageSentence(name: string, u: CategoryUsage): string {
  const parts: string[] = [];
  if (u.transactions) parts.push(plural(u.transactions, "transaction"));
  if (u.rules) parts.push(plural(u.rules, "rule"));
  if (u.budgets) parts.push("a budget");
  if (parts.length === 0) return `Nothing uses ${name}, so removing it changes nothing else.`;
  const list = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
  return `${name} is used by ${list}.`;
}

function RemovePanel({
  category,
  all,
  onClose,
  onDone,
}: {
  category: Category;
  all: Category[];
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const qc = useQueryClient();
  const fid = useId();
  const [moveTo, setMoveTo] = useState("");
  const panel = useRef<HTMLDivElement>(null);
  // The keyboard goes into the panel it opened, rather than to the top of the page.
  useEffect(() => panel.current?.focus(), []);
  const usage = useQuery({
    queryKey: ["category-usage", category.id],
    queryFn: () => api.categoryUsage(category.id),
    // Never kept once the panel closes. A remembered "nothing uses Coffee" from earlier
    // showed, with Remove enabled, while the fresh answer was still on its way -- and a
    // quick click then uncategorised transactions nobody was asked about.
    gcTime: 0,
  });
  // Anywhere a transaction can live, except the category being removed.
  const destinations = useMemo(
    () => (c: Category) => c.parent_id !== null && c.id !== category.id,
    [category.id],
  );
  const remove = useMutation({
    // The destination travels with the request, so the message afterwards names where
    // things actually went -- not whatever the picker happens to hold by then.
    mutationFn: (destination: string) => api.removeCategory(category.id, destination || null),
    onSuccess: (result, destination) => {
      const target = all.find((c) => c.id === destination)?.name ?? "Uncategorised";
      onDone(
        result.moved_transactions
          ? `Removed ${category.name}. ${plural(result.moved_transactions, "transaction")} moved to ${target}.`
          : `Removed ${category.name}.`,
      );
      // Closed and forgotten before anything refetches: the category no longer exists,
      // and asking what uses it would only answer "not found" under the message above.
      qc.removeQueries({ queryKey: ["category-usage", category.id] });
      onClose();
      // Transactions, rules, budgets, and every total built from them may have changed.
      qc.invalidateQueries();
    },
  });

  // Only an answer from THIS visit to the panel, and only once it has fully arrived.
  const u = usage.isFetching ? undefined : usage.data;
  const hasThings = !!u && (u.transactions > 0 || u.rules > 0);
  return (
    <div
      ref={panel}
      tabIndex={-1}
      className="cat-remove"
      role="group"
      aria-label={`Remove ${category.name}`}
    >
      {usage.isFetching && <p className="muted">Checking what uses {category.name}…</p>}
      {usage.isError && !usage.isFetching && (
        <div className="notice notice-warn">{errorText(usage.error)}</div>
      )}
      {u && (
        <>
          <p aria-live="polite">{usageSentence(category.name, u)}</p>
          {hasThings && (
            <div className="field">
              <label htmlFor={`${fid}-move`}>Move its transactions to</label>
              <CategoryPicker
                id={`${fid}-move`}
                categories={all}
                selectable={destinations}
                value={moveTo}
                onChange={setMoveTo}
                emptyLabel="Uncategorised"
                disabled={remove.isPending}
              />
            </div>
          )}
          <ul className="cat-remove-effects">
            {u.rules > 0 && (
              <li>
                {moveTo
                  ? `Its ${plural(u.rules, "rule")} will move with them.`
                  : `Its ${plural(u.rules, "rule")} will be removed.`}
              </li>
            )}
            {u.budgets > 0 && <li>Its budget will be removed.</li>}
          </ul>
        </>
      )}
      {remove.isError && (
        <div className="notice notice-warn" role="alert">
          {errorText(remove.error)}
        </div>
      )}
      <div className="cat-remove-actions">
        <button
          type="button"
          className="btn btn-danger"
          disabled={!u || remove.isPending}
          onClick={() => remove.mutate(moveTo)}
        >
          {remove.isPending ? "Removing…" : `Remove ${category.name}`}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={remove.isPending}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/** Puts the keyboard back on the button that opened a form, once the form closes. */
function useReturnFocus(open: boolean): RefObject<HTMLButtonElement | null> {
  const button = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) {
      wasOpen.current = true;
      return;
    }
    if (wasOpen.current) {
      wasOpen.current = false;
      button.current?.focus();
    }
  }, [open]);
  return button;
}

function AddCategory({ group, onDone }: { group: Category; onDone: (message: string) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const addButton = useReturnFocus(open);
  const add = useMutation({
    mutationFn: () => api.createCategory({ name, parent_id: group.id }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["categories"] });
      onDone(`Added ${created.name} to ${group.name}.`);
      setName("");
      setOpen(false);
    },
  });
  if (!open) {
    return (
      <button ref={addButton} type="button" className="btn btn-ghost cat-add" onClick={() => setOpen(true)}>
        + Add a category to {group.name}
      </button>
    );
  }
  return (
    <form
      className="cat-row-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) add.mutate();
      }}
    >
      <input
        aria-label={`New category in ${group.name}`}
        placeholder="Name"
        value={name}
        maxLength={80}
        autoFocus
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
      />
      <button type="submit" className="btn btn-primary" disabled={!name.trim() || add.isPending}>
        Add
      </button>
      <button type="button" className="btn btn-ghost" onClick={() => setOpen(false)}>
        Cancel
      </button>
      {add.isError && (
        <div className="notice notice-warn cat-row-error" role="alert">
          {errorText(add.error)}
        </div>
      )}
    </form>
  );
}

function AddGroup({ onDone }: { onDone: (message: string) => void }) {
  const qc = useQueryClient();
  const fid = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("expense");
  const addButton = useReturnFocus(open);
  const add = useMutation({
    mutationFn: () => api.createCategory({ name, kind }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["categories"] });
      onDone(`Added the ${created.name} group. Open it to add categories to it.`);
      setName("");
      setOpen(false);
    },
  });
  if (!open) {
    return (
      <button ref={addButton} type="button" className="btn cat-add-group" onClick={() => setOpen(true)}>
        + Add a group
      </button>
    );
  }
  return (
    <form
      className="cat-add-group-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) add.mutate();
      }}
    >
      <div className="row">
        <div className="field">
          <label htmlFor={`${fid}-name`}>Group name</label>
          <input
            id={`${fid}-name`}
            value={name}
            maxLength={80}
            autoFocus
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`${fid}-kind`}>What it holds</label>
          <select id={`${fid}-kind`} value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </div>
      </div>
      {add.isError && (
        <div className="notice notice-warn" role="alert">
          {errorText(add.error)}
        </div>
      )}
      <button type="submit" className="btn btn-primary" disabled={!name.trim() || add.isPending}>
        Add group
      </button>{" "}
      <button type="button" className="btn btn-ghost" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  );
}
