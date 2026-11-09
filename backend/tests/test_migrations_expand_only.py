"""The expand-only rule for migrations, enforced instead of merely practised.

backend/migrations/versions/ holds 0001 through 0012, and every upgrade() body in it is
additive: create_table, create_index, add_column, and one alter_column widening
import_batches.account_id to nullable. Twelve for twelve, no drops, no renames, nothing
that touches a row. That is a habit, and the habit is load-bearing.

It is load-bearing because the rollback story leans on it. app/dbinit.py tolerates a
database that is NEWER than the running image, so recovering from a bad release is "deploy
the previous image" and nothing else — the old code meets the newer schema, finds every
column it knows about still there, and keeps working. That promise holds exactly as long as
new revisions keep being additive. The first migration that drops or renames something
turns a one-command image rollback into a restore from backup, on the box holding a
family's real financial records, at the worst possible moment: the one where something has
already gone wrong.

Nothing enforced it before this file. `alembic upgrade head` and `alembic check` in
checks.yml prove the migrations run and that they agree with the models; neither has any
opinion about whether they are additive.

The first test reads all twelve migrations and refuses anything non-additive. The rest are
there because "no violations found" and "found nothing at all" are the same green, and a
checker nobody checks drifts toward the second. So the checker is built to make silence
impossible rather than merely unlikely: every statement in an upgrade() must be a shape it
recognises, and anything else is reported as "this guard cannot tell what that does". The
synthetic tests then pin each half of that down — one assertion per forbidden construct,
one migration using every allowed shape that must still pass, and one regression test per
hole an adversarial review found and this file now closes.

test_nothing_in_the_tree_runs_a_migration_backwards points the other way; its docstring
explains why the downgrade() bodies are the problem rather than the solution, and the two
regression tests that follow it are about what that scan was missing and what it was inventing.
"""

from __future__ import annotations

import ast
import pathlib
import re
import textwrap

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
VERSIONS_DIR = BACKEND / "migrations" / "versions"

# The additive allow-list, derived by reading all twelve upgrade() bodies and then
# narrowed rather than widened.
#
# What the twelve actually use is four operations: create_table (0001, 0002, 0003, 0004,
# 0006, 0007, 0011), create_index (most of them), add_column (0005, 0008, 0010, 0012), and
# a single batch_alter_table + alter_column widening account_id to nullable (0009). Unique
# and foreign-key constraints appear only as sa.UniqueConstraint/sa.ForeignKeyConstraint
# arguments inside create_table, never as standalone operations.
#
# So the list is exactly observed usage, and everything else is refused by default —
# including operations that look harmless, because the two failure modes are not
# symmetrical: refusing one wrongly costs a deliberate one-line edit here, read in review;
# permitting one wrongly costs a schema change that breaks an image rollback in production.
#
# create_primary_key is deliberately absent. It reads like a create_* sibling, but adding a
# primary key makes its columns NOT NULL and builds a unique index, so it fails outright on
# a table with rows — the opposite of additive.
#
# The list used to be three entries wider: create_unique_constraint, create_foreign_key and
# create_check_constraint, on the grounds that those are what an additive revision against
# an existing table reaches for, and that refusing them pushes a contributor toward raw SQL.
# That argument does not survive the one this file is built on. A constraint is not an
# addition from the point of view of the rolled-back image: it is a new rule that image has
# never heard of, enforced against the rows it is still writing. Add a UNIQUE constraint and
# the previous release's next legitimate duplicate is rejected by the database; add a CHECK
# and a write it made happily yesterday fails today. That is precisely the "writes that
# touch something the newer release added" hazard that dbinit's tolerance of a newer schema
# is supposed to make survivable, and a constraint is the one addition that reaches
# backwards into the old code's behaviour instead of sitting beside it. They can also fail
# outright on existing rows that violate them, which an add_column cannot do.
#
# Refusing them costs nothing today: no migration here uses any of the three. The safe
# shape is the one this file already teaches for drops, run forwards — ship the release
# that stops writing rows the constraint would reject, then add the constraint in the next
# revision, by which time the oldest image anyone would roll back to already respects it.
# If some future constraint genuinely cannot wait, the refusal is one deliberate edit here
# with a reason attached, read in review. That is the point of keeping the list narrow.
_ADDITIVE_OPERATIONS = frozenset(
    {
        "create_table",
        # create_index, but NOT with unique=True -- see _check_create_index. A unique
        # index on an existing table is a constraint wearing an index's name: it can fail
        # on the rows already there, and it rejects writes a rolled-back image makes in
        # good faith. Removing create_unique_constraint for exactly that reason and
        # leaving this open would have been a distinction without a difference.
        "create_index",
        "add_column",
    }
)

# Not operations in their own right. batch_alter_table only opens a block, and what the
# block does is judged call by call under the same allow-list. op.f renders a name from the
# naming convention and touches nothing.
_BLOCK_OPENERS = frozenset({"batch_alter_table"})
_NAME_HELPERS = frozenset({"f"})

# alter_column is judged separately: it is the one operation that can be either.
_CONDITIONAL_OPERATIONS = frozenset({"alter_column"})

# Why the obvious offenders are refused. Anything not named here and not on the allow-list
# gets the generic message, which is the right default for an operation nobody has thought
# about yet.
_REFUSED_BECAUSE = {
    "drop_table": "drops a table",
    "drop_column": "drops a column",
    "drop_index": "drops an index",
    "drop_constraint": "drops a constraint",
    "drop_table_comment": "drops a table comment",
    "rename_table": "renames a table",
    "create_primary_key": (
        "adds a primary key, which makes its columns NOT NULL and builds a unique index, "
        "so it fails on a table that already has rows"
    ),
    "create_unique_constraint": (
        "adds a UNIQUE constraint to a table that already exists. It fails outright if two "
        "rows already collide, and it keeps being enforced against an image that has never "
        "heard of it: roll back to that image and the next duplicate it legitimately writes "
        "is rejected by the database. Add the constraint in the release after the one that "
        "stopped writing duplicates, the way a drop waits one release"
    ),
    "create_check_constraint": (
        "adds a CHECK constraint to a table that already exists, with the same two problems "
        "as a UNIQUE constraint: it fails on rows that already violate it, and once it is "
        "there it rejects writes an older image makes without knowing the rule exists"
    ),
    "create_foreign_key": (
        "adds a FOREIGN KEY to a table that already exists. Postgres validates every row "
        "already there, and from then on rejects any insert whose parent row is missing — "
        "including inserts from an older image written before the parent was required"
    ),
    "execute": (
        "runs a raw statement. This is refused whether or not the statement happens to be "
        "additive: an AST cannot read SQL, a prefix check would wave through "
        "'CREATE INDEX ...; DROP TABLE ...', and Alembic has a typed operation for every "
        "additive change these twelve migrations have needed"
    ),
    "bulk_insert": "writes rows, and a schema migration must not carry data",
}

# Arguments to alter_column that describe the column as it already is, so Alembic can
# render the statement. They change nothing, so they are not what makes an alter_column
# narrowing or widening.
_ALTER_COLUMN_DESCRIPTORS = frozenset({"schema", "table_name", "column_name"})

_UNREADABLE_CALL = (
    "is not a call this guard can account for. Inside upgrade(), a call has to be an "
    "Alembic operation on the module's `op` (or on a batch block's handle), op.f(), or a "
    "sqlalchemy value being described to Alembic inside one of those. This is none of "
    "them, so the guard cannot say what it does to the schema — and in a file whose whole "
    "job is to constrain what a migration may do, 'I cannot tell' has to read as a failure "
    "rather than as a pass"
)

_UNREADABLE_STATEMENT = (
    "is not a statement shape this guard can read. An expand-only upgrade() is a flat "
    "sequence of op.<operation>(...) calls and `with op.batch_alter_table(...)` blocks, "
    "which is what all twelve migrations are. Anything else — an assignment that aliases "
    "`op`, a loop, a conditional, a nested def, an import — gives the migration a way to "
    "act that this checker cannot follow, so it is refused rather than waved through"
)


def _alembic_op_names(tree: ast.Module) -> set[str]:
    """What `op` is spelled as in this module.

    Every migration here writes `from alembic import op`, so hardcoding "op" would work
    today. Resolving it matters for the same reason the per-statement rule below exists: a
    checker that silently matches nothing reads exactly like a clean bill of health, and an
    `import ... as` away from the house style should not be the thing that produces one.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alembic":
            for alias in node.names:
                if alias.name == "op":
                    names.add(alias.asname or "op")
    return names


def _sqlalchemy_names(tree: ast.Module) -> set[str]:
    """What sqlalchemy is reachable as in this module: `sa` here, by house style.

    Needed because a create_table's arguments are full of sa.Column(...) and
    sa.ForeignKeyConstraint(...) calls. Those describe a value for Alembic to render, so
    they are allowed where they appear — but only there, which is why the name has to be
    resolved rather than assumed: see _describes_schema.
    """
    def is_sqlalchemy(module: str | None) -> bool:
        return module is not None and (
            module == "sqlalchemy" or module.startswith("sqlalchemy.")
        )

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_sqlalchemy(alias.name):
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and is_sqlalchemy(node.module):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _string_argument(call: ast.Call, *, keyword: str, position: int) -> str | None:
    """A literal string argument, by keyword or by position; None if it is not literal."""
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        value = call.args[position].value
        return value if isinstance(value, str) else None
    return None


def _unique_index_problem(call: ast.Call, created_here: set[str | None]) -> str | None:
    """A unique index on an existing table is a constraint wearing an index's name.

    create_unique_constraint, create_foreign_key and create_check_constraint were taken
    off the allow-list because each can fail on the rows already in the table, and each
    rejects writes a rolled-back image makes in good faith -- the hazard this file exists
    to bound. `op.create_index(..., unique=True)` does both of those things, under a name
    that reads as additive. Leaving it open would have been a distinction without a
    difference.

    A unique index on a table this same upgrade() creates is a different matter, and is
    allowed: the table is new, there are no existing rows, and no older image writes to a
    table it has never heard of. 0001 does exactly that with users.email.
    """
    if _string_argument(call, keyword="table_name", position=1) in created_here:
        return None
    for keyword in call.keywords:
        if keyword.arg != "unique":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and value.value is True:
            return (
                "creates a UNIQUE index on an existing table — it can fail on the rows "
                "already there, and it rejects writes an older image makes after a "
                "rollback. Add the index without `unique=True`, or add the uniqueness in "
                "a later release once the data is known to satisfy it"
            )
        if not isinstance(value, ast.Constant):
            return (
                "passes a computed value for `unique=`, which this guard cannot read; "
                "write it as a literal so the additive question has an answer"
            )
    return None


def _upgrade_binding(tree: ast.Module) -> ast.stmt | None:
    """The module-level statement that leaves `upgrade` bound to what Alembic will call.

    The question is what Python binds, not which `def` reads first, and taking the first
    `def upgrade` was a real hole rather than a nicety. Python binds the name as it
    executes the module, so with two `def upgrade` the second wins and the first is dead
    code: a checker reading the first judges a body that never runs, and passes a file
    whose live body drops a table — the exact failure this file exists to prevent, hidden
    behind a green test.

    The same reasoning covers the shapes that are not a def at all. An `async def upgrade`
    is not something Alembic would run, and `upgrade = _rename_it` or
    `from ._shared import upgrade` are not house style, but each of them is still what the
    name ends up pointing at, and this guard must not answer by judging an earlier `def`
    that has already been replaced. So the last binding wins whatever it is, and the caller
    refuses the ones it cannot read.
    """
    def binds_upgrade(node: ast.stmt) -> bool:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node.name == "upgrade"
        if isinstance(node, ast.Assign):
            return any(
                isinstance(target, ast.Name) and target.id == "upgrade"
                for target in node.targets
            )
        if isinstance(node, ast.AnnAssign | ast.AugAssign):
            return isinstance(node.target, ast.Name) and node.target.id == "upgrade"
        if isinstance(node, ast.Import | ast.ImportFrom):
            return any(
                (alias.asname or alias.name.split(".")[0]) == "upgrade"
                for alias in node.names
            )
        return False

    # Module level AND one level in. A `def upgrade` inside a module-level `if`, `try` or
    # `for` rebinds the name when the module executes, and is invisible to a walk of
    # tree.body alone -- which is a conditional teardown sitting in a migration that this
    # guard would wave through. Anything nested deeper than that is not a shape a
    # migration has, and the caller refuses what it cannot read, so there is no silent
    # gap below this.
    found: ast.stmt | None = None
    for node in tree.body:
        if binds_upgrade(node):
            found = node
            continue
        for inner in getattr(node, "body", []) + getattr(node, "orelse", []) + [
            handler_stmt
            for handler in getattr(node, "handlers", [])
            for handler_stmt in handler.body
        ]:
            if isinstance(inner, ast.stmt) and binds_upgrade(inner):
                found = inner
    return found


def _is_nothing(statement: ast.stmt) -> bool:
    """True for `pass` and for a bare constant (a docstring, `...`): a no-op either way."""
    if isinstance(statement, ast.Pass):
        return True
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _is_column_construction(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr == "Column"
    return isinstance(node.func, ast.Name) and node.func.id == "Column"


def _describes_schema(call: ast.Call, schema_names: frozenset[str]) -> bool:
    """True if this call builds a sqlalchemy value rather than doing something.

    sa.Column(...), sa.String(length=32), sa.ForeignKeyConstraint(...), sa.false(): these
    are arguments, and Alembic renders them into the statement it runs. The test is where
    the chain is rooted, so sa.dialects.postgresql.JSONB() and sa.String(32).with_variant()
    both qualify.

    This is deliberately only consulted for calls nested inside a recognised operation.
    `sa.create_engine(...)` is rooted at sa too, and a statement of its own that reaches
    the database behind Alembic's back is exactly what must not be waved through.
    """
    root: ast.AST = call.func
    while True:
        if isinstance(root, ast.Attribute):
            root = root.value
        elif isinstance(root, ast.Call):
            root = root.func
        elif isinstance(root, ast.Subscript):
            root = root.value
        else:
            break
    return isinstance(root, ast.Name) and root.id in schema_names


def _alter_column_problem(call: ast.Call) -> str | None:
    """Why this alter_column is not a widening, or None if it is one.

    The only widening Alembic's alter_column can express is making a column nullable, and
    0009 is the one migration that does it: a batch block setting
    import_batches.account_id to nullable=True. Everything else alter_column can do —
    narrowing to NOT NULL, changing a type, renaming, moving a server default — is a change
    an older image can trip over, so nullable=True on its own is the whole allowance.
    """
    if any(keyword.arg is None for keyword in call.keywords):
        return "passes **kwargs, so what it changes cannot be read here"

    arguments = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    changes = {
        name
        for name in arguments
        if not name.startswith("existing_") and name not in _ALTER_COLUMN_DESCRIPTORS
    }

    if "new_column_name" in changes:
        return (
            "renames a column; the running image still selects the old name and starts "
            "failing the moment this is applied"
        )
    if "type_" in changes:
        return "changes a column type, which can truncate values and cannot be undone"
    if "server_default" in changes:
        return (
            "changes a server default, which an older image may be relying on to supply "
            "a value it does not write itself"
        )
    if "nullable" not in changes:
        return (
            "alters a column without widening it to nullable, which is the only "
            "alter_column this rule allows"
        )

    nullable = arguments["nullable"]
    if not isinstance(nullable, ast.Constant):
        return "sets nullable to an expression this guard cannot evaluate"
    if nullable.value is False:
        return (
            "makes a column NOT NULL. That fails outright if any row holds a NULL, and it "
            "rejects the inserts an older image is still making without that column"
        )
    if nullable.value is not True:
        return f"sets nullable to {nullable.value!r} rather than True"

    narrowing_too = sorted(changes - {"nullable"})
    if narrowing_too:
        return f"changes {narrowing_too} alongside nullable=True, which is more than a widening"
    return None


def _add_column_problem(call: ast.Call) -> str | None:
    """Why this add_column is unsafe, or None if it is safe.

    Adding a column is the additive operation, with one trap: NOT NULL with no
    server_default fails on any table that already has rows, which on this box means every
    table that matters. 0005 and 0012 are the two NOT NULL additions here and both carry
    one — sa.false() and '0' respectively — so existing rows get a value and nobody is
    signed out or has a transaction rewritten by the upgrade itself.
    """
    candidates = [*call.args, *(k.value for k in call.keywords if k.arg == "column")]
    column = next((node for node in candidates if _is_column_construction(node)), None)
    if column is None:
        return (
            "this guard could not find the sa.Column(...) being added, so it cannot tell "
            "whether the column is NOT NULL without a server_default; write the Column "
            "inline so the check can read it"
        )

    arguments = {keyword.arg: keyword.value for keyword in column.keywords if keyword.arg}

    not_null = False
    if "nullable" in arguments:
        nullable = arguments["nullable"]
        if not isinstance(nullable, ast.Constant):
            return "sets nullable to an expression this guard cannot evaluate"
        not_null = nullable.value is False
    primary_key = arguments.get("primary_key")
    if isinstance(primary_key, ast.Constant) and primary_key.value is True:
        not_null = True

    if not not_null:
        return None

    default = arguments.get("server_default")
    if default is None or (isinstance(default, ast.Constant) and default.value is None):
        return (
            "adds a NOT NULL column with no server_default. Postgres has no value to put "
            "in the rows that are already there, so this fails on any non-empty table — "
            "and an older image, which does not know the column exists, cannot supply one "
            "on insert either. Give it a server_default, as 0005 and 0012 do"
        )
    return None


def expand_only_violations(source: str) -> list[str]:
    """Everything in this migration's upgrade() that is not purely additive.

    An empty list means the upgrade only adds things (and may widen a column to nullable).
    The checker walks the upgrade() body specifically: a downgrade() full of drops is
    expected and is not this function's business.

    The rule is per statement, not per file. Every statement has to be a shape this guard
    recognises — an operation call, a batch block, or a no-op — and every call inside one
    has to be an operation, op.f(), or a sqlalchemy value. Anything else is reported.
    That is what makes the twelve-migration test above non-vacuous without a counter to
    back it up: there is no way for this checker to read a file and find nothing, because
    a statement it cannot read is itself a violation. A per-file "did we recognise at least
    one operation" counter was not enough — one create_index at the top disarmed it for
    everything underneath, and four separate shapes (an alias of `op` bound by an
    assignment, a connection built straight from sqlalchemy, getattr on the operation name,
    a string handed to exec) came back with zero violations on that account.
    """
    tree = ast.parse(source)
    op_names = frozenset(_alembic_op_names(tree) or {"op"})
    schema_names = frozenset(_sqlalchemy_names(tree) or {"sa"})
    upgrade = _upgrade_binding(tree)
    if upgrade is None:
        return ["no module-level upgrade() function, so there is nothing to check"]
    if not isinstance(upgrade, ast.FunctionDef | ast.AsyncFunctionDef):
        return [
            f"line {upgrade.lineno}: {' '.join(ast.unparse(upgrade).split())} — this is "
            "what `upgrade` is bound to by the time Alembic looks for it, and it is not a "
            "def this guard can read. Write the upgrade as a function in the file, the way "
            "all twelve migrations do, so there is a body to vouch for"
        ]

    violations: list[str] = []

    # Tables this upgrade() creates itself. A constraint or a unique index on one of them
    # carries none of the hazard the rule is about: the table is new, there are no rows to
    # violate it, and no older image writes to a table it has never heard of. 0001 creates
    # `users` and indexes its email column unique in the next line, which is correct and
    # must stay correct -- so this is the difference between a rule and a nuisance.
    created_here = {
        _string_argument(node, keyword="table_name", position=0)
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_table"
    } - {None}

    def report(node: ast.AST, reason: str) -> None:
        # One line per violation: ast.unparse keeps the newlines of a multi-line call, and
        # a report that wraps is a report nobody reads to the end.
        rendered = " ".join(ast.unparse(node).split())
        if len(rendered) > 120:
            rendered = rendered[:117] + "..."
        violations.append(f"line {node.lineno}: {rendered} — {reason}")

    def operation_name(call: ast.Call, batch_names: frozenset[str]) -> str | None:
        """The method name if this call is an Alembic operation, else None."""
        if not isinstance(call.func, ast.Attribute):
            return None
        base = call.func.value
        if not isinstance(base, ast.Name):
            return None
        if base.id in op_names or base.id in batch_names:
            return call.func.attr
        return None

    def judge(call: ast.Call, name: str, batch_names: frozenset[str]) -> None:
        if name in _BLOCK_OPENERS:
            return
        if name in _CONDITIONAL_OPERATIONS:
            problem = _alter_column_problem(call)
            if problem:
                report(call, problem)
            return
        if name in _ADDITIVE_OPERATIONS:
            problem = None
            if name == "add_column":
                problem = _add_column_problem(call)
            elif name == "create_index":
                problem = _unique_index_problem(call, created_here)
            if problem:
                report(call, problem)
            return

        report(
            call,
            _REFUSED_BECAUSE.get(
                name,
                f"{name!r} is not on the additive allow-list in "
                f"{pathlib.Path(__file__).name}; if it belongs there, add it with a reason",
            ),
        )

    def check_expression(
        node: ast.AST, batch_names: frozenset[str], *, inside_operation: bool
    ) -> None:
        nested = inside_operation
        if isinstance(node, ast.Call):
            name = operation_name(node, batch_names)
            if name is None:
                if not (inside_operation and _describes_schema(node, schema_names)):
                    report(node, _UNREADABLE_CALL)
            elif name in _NAME_HELPERS:
                nested = True
            else:
                judge(node, name, batch_names)
                nested = True
        for child in ast.iter_child_nodes(node):
            check_expression(child, batch_names, inside_operation=nested)

    def check_statement(statement: ast.stmt, batch_names: frozenset[str]) -> None:
        if _is_nothing(statement):
            return

        # AsyncWith as well as With, not because a migration would ever be async, but
        # because missing the block would silently take `batch.drop_column(...)` out of
        # scope rather than failing — the one way this checker could go quiet.
        if isinstance(statement, ast.With | ast.AsyncWith):
            bound = set(batch_names)
            for item in statement.items:
                context = item.context_expr
                check_expression(context, batch_names, inside_operation=False)
                if (
                    isinstance(context, ast.Call)
                    and operation_name(context, batch_names) in _BLOCK_OPENERS
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    bound.add(item.optional_vars.id)
            for inner in statement.body:
                check_statement(inner, frozenset(bound))
            return

        if isinstance(statement, ast.Expr):
            check_expression(statement.value, batch_names, inside_operation=False)
            return

        # Not a shape the guard reads. It is reported, and then walked anyway, so that an
        # operation hidden inside it is named too rather than left to the generic message.
        report(statement, _UNREADABLE_STATEMENT)
        check_expression(statement, batch_names, inside_operation=False)

    for statement in upgrade.body:
        check_statement(statement, frozenset())
    return violations


def test_every_migration_upgrade_is_expand_only() -> None:
    migrations = sorted(VERSIONS_DIR.glob("*.py"))
    assert len(migrations) >= 12, (
        f"found {len(migrations)} migrations under {VERSIONS_DIR}, expected at least the "
        "twelve that exist — the glob is wrong and this guard would pass vacuously"
    )

    offenders: dict[str, list[str]] = {}
    for path in migrations:
        found = expand_only_violations(path.read_text())
        if found:
            offenders[path.name] = found

    report = "\n".join(
        f"  {name}:\n" + "\n".join(f"    {line}" for line in lines)
        for name, lines in sorted(offenders.items())
    )
    assert not offenders, (
        "These upgrade() bodies are not expand-only. The rule: a migration may add "
        "tables, columns and indexes, and may widen a column to nullable. It may not drop, "
        "rename or narrow anything, it may not add a constraint to a table that already "
        "exists, and it may not touch a row.\n\n"
        "The reason is the rollback path. app/dbinit.py lets an older image run against a "
        "newer database, so recovering from a bad release is 'deploy the previous image' "
        "and nothing else. A migration that drops or renames something breaks the older "
        "image against the schema it has just been rolled back under, and turns a "
        "one-command recovery into a restore from backup.\n\n"
        "What to do instead of a drop: leave the column or table where it is and stop "
        "writing to it. Drops wait one release. Once the release that stopped using it is "
        "the oldest image anyone would roll back to, a second migration removes it, and by "
        "then the drop is safe because the code that used it is gone. A rename is the same "
        "thing in three steps: add the new column, write both, drop the old one a release "
        "later. A new constraint waits the same way, for the same reason in reverse: the "
        "older image is still writing rows that have never had to satisfy it.\n\n"
        f"{report}"
    )


# --- the checker's own proof -----------------------------------------------------------

_PREAMBLE = "import sqlalchemy as sa\nfrom alembic import op\n"


def _migration(upgrade_body: str) -> str:
    body = textwrap.indent(textwrap.dedent(upgrade_body).strip("\n"), "    ")
    return f"{_PREAMBLE}\n\ndef upgrade() -> None:\n{body}\n"


def _verdict(upgrade_body: str) -> list[str]:
    return expand_only_violations(_migration(upgrade_body))


# Every allowed shape in one migration, so an over-strict checker is caught as loudly as a
# toothless one. It covers the three operations the twelve real migrations use, the batch
# block and the widening alter_column that 0009 is, both NOT NULL spellings that carry a
# server_default, op.f in an index name, and the sa.* constructors that fill a create_table.
_ADDITIVE_MIGRATION = """
op.create_table(
    'statement_shapes',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('household_id', sa.String(length=32), nullable=False),
    sa.Column('seen_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['household_id'], ['households.id']),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('household_id', 'id', name='uq_statement_shape'),
)
op.create_index(
    op.f('ix_statement_shapes_household_id'), 'statement_shapes', ['household_id']
)
op.add_column('accounts', sa.Column('nickname', sa.String(length=120), nullable=True))
op.add_column(
    'accounts', sa.Column('archived', sa.Boolean(), nullable=False, server_default=sa.false())
)
with op.batch_alter_table('accounts') as batch:
    batch.add_column(sa.Column('sort_hint', sa.Integer(), nullable=False, server_default='0'))
    batch.alter_column('institution', existing_type=sa.String(length=120), nullable=True)
"""


def test_the_checker_catches_each_forbidden_construct_and_passes_an_additive_one() -> None:
    """One assertion per construct, so a checker that half-breaks still fails loudly.

    A single table-driven loop would go green the moment the matcher stopped recognising
    any one construct, as long as the others still fired; named assertions say which.
    """
    assert _verdict("op.drop_table('budgets')"), "misses drop_table"
    assert _verdict("op.drop_column('transactions', 'category_locked')"), "misses drop_column"
    assert _verdict(
        "op.drop_index('ix_txn_account_provider', table_name='transactions')"
    ), "misses drop_index"
    assert _verdict(
        "op.drop_constraint('uq_budget_household_category', 'budgets', type_='unique')"
    ), "misses drop_constraint"
    assert _verdict("op.rename_table('budgets', 'spending_limits')"), "misses rename_table"
    assert _verdict(
        "op.alter_column('import_batches', 'account_id',"
        " existing_type=sa.String(length=32), nullable=False)"
    ), "misses an alter_column that narrows a column to NOT NULL"
    assert _verdict(
        "op.alter_column('transactions', 'merchant',"
        " existing_type=sa.String(length=160), new_column_name='payee')"
    ), "misses an alter_column that renames a column"
    assert _verdict(
        "op.alter_column('transactions', 'merchant',"
        " existing_type=sa.String(length=160), type_=sa.String(length=40))"
    ), "misses an alter_column that narrows a column type"
    assert _verdict(
        "op.alter_column('users', 'session_epoch',"
        " existing_type=sa.Integer(), server_default=None)"
    ), "misses an alter_column that drops a server default the running image relies on"
    assert _verdict(
        "op.execute(sa.text('DELETE FROM import_batches WHERE account_id IS NULL'))"
    ), "misses a DELETE, which is 0009's downgrade and the exact shape this rule exists for"
    assert _verdict(
        "op.execute('UPDATE transactions SET category_locked = true')"
    ), "misses an UPDATE passed as a bare string"
    assert _verdict(
        "op.bulk_insert(categories_table, [{'id': 'x', 'name': 'Groceries'}])"
    ), "misses bulk_insert"
    assert _verdict(
        "op.add_column('transactions',"
        " sa.Column('needs_review', sa.Boolean(), nullable=False))"
    ), "misses a NOT NULL add_column with no server_default"
    assert _verdict(
        "op.add_column('transactions',"
        " sa.Column('needs_review', sa.Boolean(), nullable=False, server_default=None))"
    ), "misses a NOT NULL add_column whose server_default is explicitly None"
    assert _verdict(
        "op.add_column('accounts', sa.Column('ledger_id', sa.String(length=32),"
        " primary_key=True))"
    ), "misses an add_column that is NOT NULL by way of primary_key=True"
    assert _verdict(
        "op.create_primary_key('pk_accounts', 'accounts', ['id'])"
    ), "misses create_primary_key, which is not additive despite the name"
    assert _verdict(
        "with op.batch_alter_table('transactions') as batch:\n"
        "    batch.drop_column('category_locked')"
    ), "misses a drop inside a batch_alter_table block"
    assert _verdict(
        "with op.batch_alter_table('import_batches') as batch:\n"
        "    batch.alter_column('account_id', existing_type=sa.String(length=32),"
        " nullable=False)"
    ), "misses a narrowing alter_column inside a batch_alter_table block"
    assert _verdict(
        "connection = op.get_bind()\n"
        "connection.execute(sa.text('DELETE FROM transactions'))"
    ), "misses an upgrade that works through the connection rather than through op"

    # And the other direction: the checker must not refuse ordinary additive work.
    assert not _verdict(_ADDITIVE_MIGRATION), (
        "the checker refuses a migration that is purely additive, so it would block the "
        f"next legitimate revision: {_verdict(_ADDITIVE_MIGRATION)}"
    )
    # A revision that only moves the head, with nothing to vouch for either way.
    assert not _verdict("pass"), "refuses an empty upgrade(), which adds nothing and is fine"


def test_finding_1_the_last_upgrade_definition_is_the_one_judged() -> None:
    """Python binds the last `def upgrade`, so that is the body that must be judged.

    The checker used to return the first one it found. Both halves of this matter: judging
    the first means a live drop_table reads green, and reporting the first means dead code
    fails a build for a body that never runs.
    """
    additive = (
        "op.add_column('accounts', sa.Column('nickname', sa.String(length=120),"
        " nullable=True))"
    )
    drop = "op.drop_table('budgets')"

    live_drop = _migration(additive) + f"\n\ndef upgrade() -> None:\n    {drop}\n"
    found = expand_only_violations(live_drop)
    assert any("drop_table" in line for line in found), (
        "judged an earlier upgrade() that Python has already rebound, so the body that "
        f"actually runs — a drop_table — went unreported: {found}"
    )

    dead_drop = _migration(drop) + f"\n\ndef upgrade() -> None:\n    {additive}\n"
    assert not expand_only_violations(dead_drop), (
        "reported a drop in an upgrade() that a later definition has replaced, which is "
        f"dead code and cannot run: {expand_only_violations(dead_drop)}"
    )

    # The same question — what is `upgrade` bound to when Alembic calls it — has answers
    # that are not a def. Each of these leaves the checked body unreachable, so a guard
    # that judged the def above it would be vouching for code that never runs.
    rebound = _migration(additive) + "\n\nupgrade = _apply_it\n"
    assert expand_only_violations(rebound), (
        "judged a def that a module-level assignment has already replaced"
    )
    imported = _migration(additive) + "\n\nfrom ._shared import upgrade\n"
    assert expand_only_violations(imported), (
        "judged a def that a later import has replaced"
    )


# The four shapes that came back with zero violations while non-vacuity was a per-module
# counter: each contains one recognised additive operation, which was enough to disarm the
# counter for everything after it. None of the four is an Alembic operation the old matcher
# could see, which was read as "nothing to object to" rather than as "no idea what this is".
_SHAPES_THAT_DISARMED_THE_OLD_COUNTER = {
    "an alias of op bound by an assignment": (
        "op.add_column('accounts', sa.Column('nickname', sa.String(length=120),"
        " nullable=True))\n"
        "helper = op\n"
        "helper.drop_table('budgets')"
    ),
    "a connection built straight from sqlalchemy": (
        "op.create_index('ix_accounts_nickname', 'accounts', ['nickname'])\n"
        "engine = sa.create_engine('postgresql+psycopg://saiva@localhost/saiva')\n"
        "engine.execute('DROP TABLE budgets')"
    ),
    "the operation name assembled at run time": (
        "op.create_index('ix_accounts_nickname', 'accounts', ['nickname'])\n"
        "getattr(op, 'drop_' + 'table')('budgets')"
    ),
    "a string handed to exec": (
        "op.add_column('accounts', sa.Column('nickname', sa.String(length=120),"
        " nullable=True))\n"
        "exec(\"op.drop_table('budgets')\")"
    ),
}


def test_finding_2_vacuity_is_judged_per_statement_not_per_file() -> None:
    """One recognised operation must not vouch for the statements around it.

    The old guard counted recognised operations per module and only complained if the
    count was zero, so a single create_index at the top of a file silenced it. The rule is
    now per statement: a statement the checker cannot read is a violation on its own,
    because "this checker does not know what that does" is not the same as "that is fine".
    """
    for description, body in _SHAPES_THAT_DISARMED_THE_OLD_COUNTER.items():
        found = _verdict(body)
        assert found, (
            f"{description}: a recognised operation elsewhere in the file still vouches "
            "for a statement the checker cannot read"
        )
        assert any("cannot" in line or "not a" in line for line in found), (
            f"{description}: reported something, but not that the checker cannot read it: "
            f"{found}"
        )

    # The flip side, and the reason this is a rule about statements rather than a longer
    # list of forbidden names: the guard must say so when it cannot read a statement, even
    # when nothing in the statement looks dangerous.
    assert _verdict(
        "op.create_index('ix_accounts_nickname', 'accounts', ['nickname'])\n"
        "print('migrated')"
    ), "waves through a statement it cannot account for because it looks harmless"


def test_finding_3_a_constraint_added_to_an_existing_table_is_refused() -> None:
    """The three standalone constraint operations are off the allow-list deliberately.

    They are refused because of the promise this file defends, not despite it: a UNIQUE,
    CHECK or FOREIGN KEY constraint added to a table that already exists is enforced
    against an image that has never heard of it, so rolling back to that image leaves it
    making writes the database now rejects. The long comment on _ADDITIVE_OPERATIONS has
    the full argument. No migration in the tree uses any of the three, so this costs
    nothing today — and if that changes, this test is where the decision gets revisited.
    """
    assert _verdict(
        "op.create_unique_constraint('uq_budget_household_category', 'budgets',"
        " ['household_id', 'category_id'])"
    ), "allows a UNIQUE constraint on an existing table"
    assert _verdict(
        "op.create_check_constraint('ck_budget_limit_positive', 'budgets',"
        " 'limit_cents > 0')"
    ), "allows a CHECK constraint on an existing table"
    assert _verdict(
        "op.create_foreign_key('fk_budget_household', 'budgets', 'households',"
        " ['household_id'], ['id'])"
    ), "allows a FOREIGN KEY on an existing table"

    # The same constraints declared inside a create_table are untouched, because the table
    # is new: there are no existing rows to fail on and no older image writing to it.
    assert not _verdict(
        "op.create_table(\n"
        "    'budget_notes',\n"
        "    sa.Column('id', sa.String(length=32), nullable=False),\n"
        "    sa.Column('budget_id', sa.String(length=32), nullable=False),\n"
        "    sa.ForeignKeyConstraint(['budget_id'], ['budgets.id']),\n"
        "    sa.PrimaryKeyConstraint('id'),\n"
        "    sa.UniqueConstraint('budget_id', name='uq_budget_note'),\n"
        "    sa.CheckConstraint(\"id <> ''\", name='ck_budget_note_id'),\n"
        ")"
    ), "refuses constraints declared inside the create_table that makes the table"


# --- the downgrade() bodies ------------------------------------------------------------

# The two ways a downgrade gets invoked: the CLI, and the Python API that app/dbinit.py and
# test_dbinit.py already use to reach Alembic. Both are matched against the whole file
# rather than line by line, because both can be spelled across lines and were being missed:
# `alembic \` with `downgrade -1` on the next line, a YAML folded block that puts the two
# words on separate lines, and `command.downgrade \` with its arguments below it all run a
# migration backwards and all read as nothing to a per-line search.
#
# The gap the CLI pattern allows between the two words is bounded and may not contain a
# command terminator or a blank line, so it stays inside one invocation instead of pairing
# an `alembic` near the top of a workflow with the word `downgrade` far below it.
_RUNS_A_DOWNGRADE = (
    # alembic downgrade <rev>, with any flags or line continuations in between.
    re.compile(r"\balembic\b(?:(?![;&|]|\n[ \t]*\n)[\s\S]){0,120}?\bdowngrade\b"),
    # command.downgrade(cfg, rev), and aliases of it, with the parenthesis anywhere after.
    re.compile(r"\b[\w.]*downgrade[\s\\]*\("),
    # The Python API named without being called, which is how it would be stashed in a
    # variable and invoked out of sight of the pattern above.
    re.compile(r"\b(?:alembic\s*\.\s*)?command\s*\.\s*downgrade\b"),
)

# `def downgrade` is a definition, not an invocation, and all twelve migrations have one.
_A_DEFINITION = re.compile(r"\bdef[ \t]+$")

_A_COMMENT_LINE = re.compile(r"^[ \t]*#")


# What a blanked-out comment leaves behind. NOT a space, and that is the whole point.
#
# The cross-line invocation patterns treat a BLANK line as a command terminator, because
# one invocation does not span a paragraph break. Blanking a full-line comment to spaces
# therefore manufactured a terminator in the middle of a multi-line call, and a comment
# sitting inside one made the invocation invisible -- a silent miss introduced by the fix
# for the opposite problem. A NUL is not whitespace, so it does not break the run; it
# matches nothing in any pattern here; and it is one character, so every offset after it
# stays where it was and a hit is still reported against the right line.
_BLANK = "\x00"


def _blanked(text: str, start: int, end: int) -> str:
    """`text` with [start:end) replaced by a filler that is neither code nor whitespace.

    Replacing rather than deleting is what lets a hit still be reported against the right
    line of the real file: every offset stays where it was.
    """
    return (
        text[:start]
        + "".join("\n" if char == "\n" else _BLANK for char in text[start:end])
        + text[end:]
    )


def _without_comments(text: str) -> str:
    """The same text with the contents of `#` comments blanked out.

    Line by line, and quote-aware within a line, so `grep '#'` keeps its argument and a
    URL fragment in a string is not read as the start of a comment. Deliberately not
    stateful across lines: one unbalanced quote in a shell script would otherwise desync
    the rest of the file, and this guard going quiet is the failure that matters.
    """
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        cut: int | None = None
        if _A_COMMENT_LINE.match(line):
            cut = len(line) - len(line.lstrip(" \t"))
        else:
            quote: str | None = None
            position = 0
            while position < len(line):
                char = line[position]
                if quote is not None:
                    if char == "\\":
                        position += 2
                        continue
                    if char == quote:
                        quote = None
                elif char in "\"'":
                    quote = char
                elif char == "#":
                    cut = position
                    break
                position += 1
        if cut is not None:
            lines[index] = _blanked(line, cut, len(line))
    return "".join(lines)


def _without_python_prose(text: str) -> str:
    """The same text with Python docstrings and other bare string statements blanked.

    This is the "a mention is not a call" half of the fix, and it stops where it does on
    purpose. A string standing alone as a statement is documentation — it is how this file
    explains that `alembic downgrade -1` is the thing not to run. A string in an argument
    list is data, and the data in this tree includes command lines: blanking every string
    literal would hide subprocess.run(['alembic', 'downgrade', '-1']), which is exactly the
    invocation this test exists to catch. So prose goes and arguments stay.

    Unparseable input gets nothing blanked, which errs toward reporting.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text

    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    redacted = text
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            end_lineno = node.end_lineno or node.lineno
            end_col = node.end_col_offset or 0
            redacted = _blanked(
                redacted,
                starts[node.lineno - 1] + node.col_offset,
                starts[end_lineno - 1] + end_col,
            )
    return redacted


def downgrade_invocations(text: str, *, python: bool = False) -> list[str]:
    """The lines in `text` that run a migration backwards, as `line N: <source>`.

    Comments are stripped first, and in Python so is prose standing alone as a string,
    because this check is worth nothing if it cries wolf: a test that fires on a comment
    warning people off downgrades is a test everyone learns to ignore, and an ignored guard
    is the same as no guard.

    The limit is the usual one for a grep: a command assembled at runtime out of a
    variable, or a method reached through getattr, would slip past it.
    """
    redacted = _without_comments(text)
    if python:
        redacted = _without_python_prose(redacted)

    lines = text.splitlines()
    hits: dict[int, str] = {}
    for pattern in _RUNS_A_DOWNGRADE:
        for match in pattern.finditer(redacted):
            if _A_DEFINITION.search(redacted[: match.start()]):
                continue
            lineno = redacted.count("\n", 0, match.start()) + 1
            source = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            hits.setdefault(lineno, f"line {lineno}: {source}")
    return [hits[lineno] for lineno in sorted(hits)]


# Where a downgrade could plausibly be invoked: the code the container runs, the scripts the
# owner runs by hand, the workflows CI runs, and the compose/Dockerfile entrypoints that
# decide what happens at boot.
_PLACES_THAT_RUN_THINGS = (
    "backend/app/**/*.py",
    "backend/Dockerfile",
    "scripts/**/*",
    ".github/workflows/*.yml",
    "docker-compose*.yml",
    "Makefile",
)

# Two test trees, not one. backend/tests falls outside the globs above already; scripts/
# does not, and scripts/tests/ holds the shell suite — including the one that seeds an
# alembic_version row and the one that exercises deploy.sh's --allow-downgrade flag. A test
# that drives a downgrade against a throwaway database is a good thing to have, and reading
# it as production code would either report it or teach someone to weaken the pattern until
# it stopped. So any path with a `tests` component is skipped, wherever the tree grows one.
_A_TEST_TREE = frozenset({"test", "tests"})


def _is_test_support(path: pathlib.Path) -> bool:
    return any(part in _A_TEST_TREE for part in path.relative_to(REPO).parts)


def test_nothing_in_the_tree_runs_a_migration_backwards() -> None:
    """The downgrade() bodies are documentation, and must not become a procedure.

    All twelve have one, because that is what `alembic revision --autogenerate` writes, and
    not one of them has ever been executed by anything: checks.yml runs `alembic upgrade
    head` and `alembic check`, refresh-images.yml upgrades to the previous revision on a
    fresh database, and dbinit only ever upgrades.

    Grep for 'downgrade' across the tree -- excluding .git, .venv, node_modules,
    __pycache__, migrations/ and this file -- and there are twelve hits, none of them a
    migration going backwards. The exclusions are part of the number, which is why they
    are written out: quoting a count without them is a claim nobody can reproduce. Most
    are prose in comments, one is an eslint rule that was deliberately not downgraded,
    one is the HSTS max-age in the Caddyfile, and the rest are deploy.sh's
    --allow-downgrade flag and the tests of it — which is about going back to an older
    image, the recovery this file's expand-only rule is what makes safe. That shape matters
    for how this is matched: the word alone is useless as a signal, and the flag that names
    the supported recovery must not be mistaken for the unsupported one.

    An untested path is worse than no path, because it looks like one. 0009's downgrade
    opens with `DELETE FROM import_batches WHERE account_id IS NULL` — it is correct about
    the problem (a multi-account batch has no single account to go back to) and it deletes
    real import history to solve it, and it would be reached by someone already in trouble
    and reading `alembic downgrade -1` as the obvious undo. The same person's actual
    recovery path does not need it: roll the image back and leave the schema forward, which
    is what the expand-only rule above buys and what app/dbinit.py is built to allow.

    So this test does not try to make the downgrades work. It keeps them out of the
    deploy. If a rollback procedure ever does need to move the schema backwards, that is a
    deliberate change: delete this test, and in the same commit make CI actually run the
    downgrade chain against a seeded database, because until something executes those
    twelve bodies nobody knows whether they work.
    """
    # What it must catch, including the spellings that span lines.
    assert downgrade_invocations("alembic downgrade -1"), "misses the plain CLI form"
    assert downgrade_invocations(
        "alembic -c alembic.ini downgrade base"
    ), "misses the CLI form with flags in between"
    assert downgrade_invocations(
        "    command.downgrade(cfg, '0011')\n", python=True
    ), "misses the Python API, which is how dbinit reaches Alembic"
    assert downgrade_invocations(
        "run: |\n  alembic \\\n    downgrade -1\n"
    ), "misses a CLI invocation continued onto the next line"
    assert downgrade_invocations(
        "      run: >\n        alembic\n        downgrade base\n"
    ), "misses a CLI invocation folded across lines by YAML"
    assert downgrade_invocations(
        "command.downgrade \\\n    (cfg, '0011')\n", python=True
    ), "misses a Python call whose arguments are on the next line"
    assert downgrade_invocations(
        "runner = command.downgrade\nrunner(cfg, '0011')\n", python=True
    ), "misses the Python API stashed in a variable and called out of sight"
    assert downgrade_invocations(
        "subprocess.run(['alembic', 'downgrade', '-1'], check=True)\n", python=True
    ), "misses a shelled-out downgrade, whose words are all string literals"

    # What it must not cry wolf on: a definition, and prose that names the thing.
    assert not downgrade_invocations(
        "def downgrade() -> None:\n    op.drop_table('budgets')\n", python=True
    ), "fires on a migration defining its own downgrade, which every one of them does"
    assert not downgrade_invocations(
        "# code as `:latest`. That is a downgrade of a family's finance box, applied"
    ), "fires on the word in a comment, which is refresh-images.yml line 131"
    assert not downgrade_invocations(
        "    --allow-downgrade) ALLOW_DOWNGRADE=1 ;;"
    ), "fires on deploy.sh's flag for rolling back to an older image"

    scanned: dict[str, int] = {}
    callers: dict[str, list[str]] = {}
    for pattern in _PLACES_THAT_RUN_THINGS:
        scanned[pattern] = 0
        for path in sorted(REPO.glob(pattern)):
            if not path.is_file() or _is_test_support(path):
                continue
            scanned[pattern] += 1
            hits = downgrade_invocations(
                path.read_text(errors="replace"), python=path.suffix == ".py"
            )
            if hits:
                callers[str(path.relative_to(REPO))] = hits

    # Per glob, not in total, because 61 files under backend/app would otherwise hide a
    # pattern that has stopped matching anything at all.
    empty = sorted(name for name, count in scanned.items() if not count)
    assert not empty, (
        f"these globs matched no files: {empty}. Something has been renamed or moved, and "
        "this guard is no longer looking at the place it is named for"
    )
    assert sum(scanned.values()) > 60, (
        f"only scanned {sum(scanned.values())} files across {_PLACES_THAT_RUN_THINGS}, "
        "which is fewer than this tree holds — 81 as of this writing, 61 of them under "
        "backend/app. The globs are wrong and this guard would pass vacuously"
    )
    assert not callers, (
        "Something here runs a migration backwards. No downgrade() in "
        "backend/migrations/versions/ has ever been executed by anything, so none of them "
        "is known to work, and 0009's deletes import history. Roll the image back instead "
        "and leave the schema forward — app/dbinit.py is built to run an older image "
        "against a newer database, which is what the expand-only rule in this file buys. "
        "If the schema really must move backwards, make CI run the downgrade chain against "
        "a seeded database first, then delete this test in the same commit: "
        f"{callers}"
    )


def test_finding_4_a_downgrade_spelled_across_lines_is_still_caught() -> None:
    """One invocation, several lines: a per-line search saw nothing in either of these.

    The scan reads whole files now. These two are the shapes a line-based search cannot
    see at all — the words of one command split by a continuation — and both are things
    somebody would really write.
    """
    continued_cli = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "docker compose exec api alembic \\\n"
        "    -c alembic.ini \\\n"
        "    downgrade -1\n"
    )
    continued_call = (
        "from alembic import command\n"
        "\n"
        "def rollback(cfg):\n"
        "    command.downgrade \\\n"
        "        (cfg, '0011')\n"
    )
    assert downgrade_invocations(continued_cli), (
        "misses a CLI downgrade continued across lines with backslashes"
    )
    assert downgrade_invocations(continued_call, python=True), (
        "misses a Python downgrade whose argument list starts on the next line"
    )

    # The bound on the gap is what keeps this from pairing any two distant words: an
    # alembic command at the top and the word downgrade much further down are not one
    # invocation, and reporting them as one would be the cry-wolf failure again.
    assert not downgrade_invocations(
        "- run: alembic upgrade head\n"
        "\n"
        "- name: notes\n"
        "  run: echo 'rolling back is an image downgrade, not a schema one'\n"
    ), "pairs an unrelated alembic command with the word downgrade lines later"


def test_finding_5_prose_that_names_downgrade_is_not_an_invocation() -> None:
    """A mention is not a call, and a guard that cries wolf is a guard nobody reads.

    The pattern needs parentheses to count a Python call, which means prose that writes
    `downgrade()` the way prose does used to trip it. Comments and standalone strings are
    stripped before matching now, and the real call on a line that also carries a comment
    still has to be caught.
    """
    assert not downgrade_invocations(
        "# Never call downgrade() from here: see test_migrations_expand_only.py\n",
        python=True,
    ), "fires on a comment naming downgrade() with parentheses"
    assert not downgrade_invocations(
        '"""Recovery is an image rollback.\n'
        "\n"
        "Do not reach for `alembic downgrade -1`, and do not call command.downgrade(cfg)\n"
        "from the app: no downgrade() body here has ever been executed.\n"
        '"""\n',
        python=True,
    ), "fires on a module docstring explaining what not to do"
    assert not downgrade_invocations(
        "run_update()  # not a downgrade(); the schema only moves forward\n", python=True
    ), "fires on a trailing comment naming downgrade()"

    # And it must still see the real thing when prose is sitting next to it.
    assert downgrade_invocations(
        "command.downgrade(cfg, '0011')  # never call downgrade() from here\n", python=True
    ), "stops seeing a real call because there is a comment on the same line"
    assert downgrade_invocations(
        '"""Roll the schema back."""\n'
        "\n"
        "def rollback(cfg):\n"
        "    command.downgrade(cfg, '0011')\n",
        python=True,
    ), "stops seeing a real call in a module that has a docstring"


def test_finding_6_a_second_test_tree_is_not_scanned_as_production_code() -> None:
    """scripts/tests/ is a test tree, and the exclusion has to cover it.

    The comment above _PLACES_THAT_RUN_THINGS used to say the globs reached 75 files with
    4 under scripts/; they reach 85, 13 of them under scripts/, because scripts/tests/
    holds the shell suite. Scanning it as production code puts a test that drives a
    downgrade on a throwaway database in the same bucket as the deploy, and the only ways
    out of that are to weaken the pattern or to ignore the test.
    """
    assert _is_test_support(REPO / "scripts" / "tests" / "run.sh")
    assert _is_test_support(REPO / "backend" / "tests" / "test_dbinit.py")
    assert not _is_test_support(REPO / "scripts" / "deploy.sh")
    assert not _is_test_support(REPO / "scripts" / "lib" / "release.sh")
    assert not _is_test_support(REPO / "backend" / "app" / "dbinit.py")

    under_scripts = [
        path
        for path in sorted(REPO.glob("scripts/**/*"))
        if path.is_file() and not _is_test_support(path)
    ]
    assert under_scripts, "the scripts/ glob stopped matching anything outside its tests"
    assert not any(
        "tests" in path.relative_to(REPO).parts for path in under_scripts
    ), f"the scripts/ test tree is still being scanned: {under_scripts}"

    # The claim the old comment got wrong, kept honest by asserting it rather than writing
    # it down: scripts/tests/ exists and is big enough to matter.
    in_the_test_tree = [
        path
        for path in sorted(REPO.glob("scripts/**/*"))
        if path.is_file() and _is_test_support(path)
    ]
    assert len(in_the_test_tree) >= 4, (
        "scripts/tests/ has fewer files than the exclusion was written for; if the shell "
        f"suite has moved, move the exclusion with it: {in_the_test_tree}"
    )


# --------------------------------------------------------------------------------------
# Three holes a second review found in the fixes above
# --------------------------------------------------------------------------------------


def test_an_upgrade_redefined_inside_a_block_is_the_one_judged() -> None:
    """`_upgrade_binding` walked tree.body only, so one level of nesting hid a teardown.

    A `def upgrade` inside a module-level `if` or `try` rebinds the name when the module
    executes. Alembic then calls that one. A guard that reads only the top level judges a
    body that has already been replaced and passes the file -- the same class of miss as
    judging the first of two defs, one level down.
    """
    hidden = (
        "import os\n"
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "\n"
        "def upgrade() -> None:\n"
        "    op.add_column('accounts', sa.Column('nickname', sa.String(64), nullable=True))\n"
        "\n"
        "if os.environ.get('TEARDOWN'):\n"
        "    def upgrade() -> None:\n"
        "        op.drop_table('accounts')\n"
    )
    assert any("drops a table" in v for v in expand_only_violations(hidden)), (
        "a conditional redefinition of upgrade() hid a drop from the guard"
    )

    in_a_try = (
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "\n"
        "def upgrade() -> None:\n"
        "    op.add_column('accounts', sa.Column('nickname', sa.String(64), nullable=True))\n"
        "\n"
        "try:\n"
        "    def upgrade() -> None:\n"
        "        op.drop_column('accounts', 'email')\n"
        "except Exception:\n"
        "    pass\n"
    )
    assert any("drops a column" in v for v in expand_only_violations(in_a_try))


def test_a_unique_index_on_an_existing_table_is_refused() -> None:
    """The hole left by removing the three constraint operations.

    `create_unique_constraint` was taken off the allow-list because it can fail on the
    rows already in the table and because it rejects writes a rolled-back image makes in
    good faith. `op.create_index(..., unique=True)` does both of those, under a name that
    reads as additive. Refusing one and allowing the other is a distinction without a
    difference.
    """
    head = "import sqlalchemy as sa\nfrom alembic import op\n\ndef upgrade() -> None:\n    "

    refused = expand_only_violations(
        head + "op.create_index('uq_a', 'accounts', ['email'], unique=True)\n"
    )
    assert any("UNIQUE index on an existing table" in v for v in refused)

    # By keyword as well as by position, because the guard reads arguments and not text.
    by_keyword = expand_only_violations(
        head + "op.create_index('uq_a', table_name='accounts', columns=['e'], unique=True)\n"
    )
    assert by_keyword, "the table named by keyword slipped past"

    # And the two that must still be allowed, or this is a nuisance rather than a rule.
    assert not expand_only_violations(
        head + "op.create_index('ix_a', 'accounts', ['email'])\n"
    ), "a plain index on an existing table is additive"
    assert not expand_only_violations(
        head
        + "op.create_table('widgets', sa.Column('id', sa.String(32), primary_key=True))\n"
        + "    op.create_index('uq_w', 'widgets', ['id'], unique=True)\n"
    ), "a unique index on a table this upgrade creates has no existing rows to fail on"


def test_a_comment_inside_a_multi_line_invocation_does_not_hide_it() -> None:
    """A regression the comment-blanking fix introduced, caught by the next review.

    The cross-line patterns treat a blank line as a command terminator, because one
    invocation does not span a paragraph break. Blanking a full-line comment to SPACES
    manufactured exactly that terminator inside a multi-line call, so a comment sitting
    in the middle of one made the invocation invisible. The filler is now a character
    that is neither code nor whitespace.
    """
    with_comment = (
        "subprocess.run([\n"
        '    "alembic",\n'
        "    # roll it back one, as the runbook says\n"
        '    "downgrade",\n'
        '    "-1",\n'
        "])\n"
    )
    assert downgrade_invocations(with_comment, python=True), (
        "a comment inside the call hid the invocation"
    )

    # The two halves this balances between, asserted together so neither can be traded
    # away for the other.
    without_comment = with_comment.replace(
        "    # roll it back one, as the runbook says\n", ""
    )
    assert downgrade_invocations(without_comment, python=True)
    prose = "# Only ever upgrade. Never call downgrade() from here.\nrun(['alembic', 'upgrade'])\n"
    assert not downgrade_invocations(prose, python=True), "prose is not an invocation"

