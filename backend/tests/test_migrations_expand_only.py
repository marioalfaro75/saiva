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

Two tests carry the rule, and the second is what makes the first worth believing. The first
reads all twelve migrations and refuses anything non-additive. The second feeds the checker
a synthetic migration per forbidden construct and asserts each is caught, and a synthetic
additive migration that must be accepted — so a checker that quietly stops recognising
`drop_column`, or one that has grown too strict to let an ordinary add_column through,
fails loudly instead of reading green. A checker like this is worthless without that
second test: "no violations found" and "found nothing at all" are the same green.

The third test points the other way; its docstring explains why the downgrade() bodies are
the problem rather than the solution.
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
# The list below is a little wider than observed usage — it also admits the three
# standalone constraint-adding operations, because those are what an additive revision
# against an existing table reaches for, and refusing them would push a contributor toward
# raw SQL, which is worse. It is no wider than that. Everything else is refused by default,
# including operations that look harmless, because the two failure modes are not
# symmetrical: refusing one wrongly costs a deliberate one-line edit here, read in review;
# permitting one wrongly costs a schema change that breaks an image rollback in production.
#
# create_primary_key is deliberately absent. It reads like a create_* sibling, but adding a
# primary key makes its columns NOT NULL and builds a unique index, so it fails outright on
# a table with rows — the opposite of additive.
_ADDITIVE_OPERATIONS = frozenset(
    {
        "create_table",
        "create_index",
        "add_column",
        "create_unique_constraint",
        "create_foreign_key",
        "create_check_constraint",
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


def _alembic_op_names(tree: ast.Module) -> set[str]:
    """What `op` is spelled as in this module.

    Every migration here writes `from alembic import op`, so hardcoding "op" would work
    today. Resolving it matters for the same reason the non-vacuity test exists: a checker
    that silently matches nothing reads exactly like a clean bill of health, and an
    `import ... as` away from the house style should not be the thing that produces one.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alembic":
            for alias in node.names:
                if alias.name == "op":
                    names.add(alias.asname or "op")
    return names


def _upgrade_function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    return None


def _is_a_no_op_body(function: ast.FunctionDef) -> bool:
    """True for `pass` and for a docstring on its own: nothing to vouch for either way."""
    for statement in function.body:
        if isinstance(statement, ast.Pass):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        return False
    return True


def _is_column_construction(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr == "Column"
    return isinstance(node.func, ast.Name) and node.func.id == "Column"


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
    """
    tree = ast.parse(source)
    op_names = _alembic_op_names(tree) or {"op"}
    upgrade = _upgrade_function(tree)
    if upgrade is None:
        return ["no module-level upgrade() function, so there is nothing to check"]

    violations: list[str] = []
    operations_seen = 0

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

    def refuse(call: ast.Call, reason: str) -> None:
        rendered = ast.unparse(call)
        if len(rendered) > 120:
            rendered = rendered[:117] + "..."
        violations.append(f"line {call.lineno}: {rendered} — {reason}")

    def judge(call: ast.Call, batch_names: frozenset[str]) -> None:
        nonlocal operations_seen
        name = operation_name(call, batch_names)
        if name is None or name in _NAME_HELPERS:
            return

        operations_seen += 1

        if name in _BLOCK_OPENERS:
            return
        if name in _CONDITIONAL_OPERATIONS:
            problem = _alter_column_problem(call)
            if problem:
                refuse(call, problem)
            return
        if name in _ADDITIVE_OPERATIONS:
            problem = _add_column_problem(call) if name == "add_column" else None
            if problem:
                refuse(call, problem)
            return

        refuse(
            call,
            _REFUSED_BECAUSE.get(
                name,
                f"{name!r} is not on the additive allow-list in "
                f"{pathlib.Path(__file__).name}; if it belongs there, add it with a reason",
            ),
        )

    def visit(node: ast.AST, batch_names: frozenset[str]) -> None:
        # AsyncWith as well as With, not because a migration would ever be async, but
        # because missing the block would silently take `batch.drop_column(...)` out of
        # scope rather than failing — the one way this checker could go quiet.
        if isinstance(node, ast.With | ast.AsyncWith):
            bound = set(batch_names)
            for item in node.items:
                visit(item.context_expr, batch_names)
                context = item.context_expr
                if (
                    isinstance(context, ast.Call)
                    and operation_name(context, batch_names) in _BLOCK_OPENERS
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    bound.add(item.optional_vars.id)
            for statement in node.body:
                visit(statement, frozenset(bound))
            return
        if isinstance(node, ast.Call):
            judge(node, batch_names)
        for child in ast.iter_child_nodes(node):
            visit(child, batch_names)

    for statement in upgrade.body:
        visit(statement, frozenset())

    if not operations_seen and not _is_a_no_op_body(upgrade):
        violations.append(
            "nothing in this upgrade() was recognised as an Alembic operation, so this "
            "guard cannot vouch for it — it does its work some way the checker in "
            f"{pathlib.Path(__file__).name} cannot see"
        )
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
        "tables, columns, indexes and constraints, and may widen a column to nullable. It "
        "may not drop, rename or narrow anything, and it may not touch a row.\n\n"
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
        "later.\n\n"
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
# toothless one. It covers the four operations the twelve real migrations use plus the
# three standalone constraint operations the allow-list admits, both NOT NULL spellings
# that carry a server_default, op.f in an index name, and the batch-block equivalents.
_ADDITIVE_MIGRATION = """
op.create_table(
    'statement_shapes',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('household_id', sa.String(length=32), nullable=False),
    sa.Column('seen_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['household_id'], ['households.id']),
    sa.PrimaryKeyConstraint('id'),
)
op.create_index(
    op.f('ix_statement_shapes_household_id'), 'statement_shapes', ['household_id']
)
op.create_unique_constraint('uq_statement_shape', 'statement_shapes', ['household_id', 'id'])
op.create_foreign_key(
    'fk_statement_shape_household', 'statement_shapes', 'households', ['household_id'], ['id']
)
op.create_check_constraint('ck_statement_shape_id', 'statement_shapes', "id <> ''")
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


# --- the downgrade() bodies ------------------------------------------------------------

# The two ways a downgrade gets invoked: the CLI, and the Python API that app/dbinit.py and
# test_dbinit.py already use to reach Alembic. Matched as invocations rather than as the
# bare word on purpose — refresh-images.yml uses "downgrade" in prose, and a word search
# would report that comment as a violation and teach everyone to ignore this test.
_RUNS_A_DOWNGRADE = re.compile(
    r"\balembic\b[^\n]*\bdowngrade\b"  # alembic downgrade <rev>, with any flags between
    r"|(?<!def )\b[\w.]*downgrade\s*\("  # command.downgrade(cfg, rev), and aliases of it
)

# Where a downgrade could plausibly be invoked: the code the container runs, the scripts the
# owner runs by hand, the workflows CI runs, and the compose/Dockerfile entrypoints that
# decide what happens at boot. Not backend/tests: a test that exercises a downgrade on a
# throwaway database is fine, and would be a good thing to have.
_PLACES_THAT_RUN_THINGS = (
    "backend/app/**/*.py",
    "backend/Dockerfile",
    "scripts/**/*",
    ".github/workflows/*.yml",
    "docker-compose*.yml",
    "Makefile",
)


def test_nothing_in_the_tree_runs_a_migration_backwards() -> None:
    """The downgrade() bodies are documentation, and must not become a procedure.

    All twelve have one, because that is what `alembic revision --autogenerate` writes, and
    not one of them has ever been executed by anything: checks.yml runs `alembic upgrade
    head` and `alembic check`, refresh-images.yml upgrades to the previous revision on a
    fresh database, and dbinit only ever upgrades. Grep for 'downgrade' across the tree and
    the only hit outside migrations/ is the word in a comment.

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

    The limit of this check is the usual one for a grep: a command assembled at runtime out
    of a variable would slip past it.
    """
    assert _RUNS_A_DOWNGRADE.search("alembic downgrade -1"), "misses the plain CLI form"
    assert _RUNS_A_DOWNGRADE.search(
        "alembic -c alembic.ini downgrade base"
    ), "misses the CLI form with flags in between"
    assert _RUNS_A_DOWNGRADE.search(
        "    command.downgrade(cfg, '0011')"
    ), "misses the Python API, which is how dbinit reaches Alembic"
    assert not _RUNS_A_DOWNGRADE.search(
        "def downgrade() -> None:"
    ), "fires on a migration defining its own downgrade, which every one of them does"
    assert not _RUNS_A_DOWNGRADE.search(
        "# code as `:latest`. That is a downgrade of a family's finance box, applied"
    ), "fires on the word in a comment, which is refresh-images.yml line 124"

    scanned = 0
    callers: dict[str, list[str]] = {}
    for pattern in _PLACES_THAT_RUN_THINGS:
        for path in sorted(REPO.glob(pattern)):
            if not path.is_file():
                continue
            scanned += 1
            hits = [
                line.strip()
                for line in path.read_text(errors="replace").splitlines()
                if _RUNS_A_DOWNGRADE.search(line)
            ]
            if hits:
                callers[str(path.relative_to(REPO))] = hits

    assert scanned > 20, (
        f"only scanned {scanned} files across {_PLACES_THAT_RUN_THINGS}, which is fewer "
        "than this tree holds — the globs are wrong and this guard would pass vacuously"
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
