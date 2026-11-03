"""Guards against the defect that failed CI run 41364727610.

Five test modules used to open with `TODAY = dt.date.today()`. pytest evaluates that
while it *collects*, minutes before the last assertion runs, while the app reads its
own date per request — so any run straddling midnight compared a stale constant
against a fresh date and failed on a mismatch that had nothing to do with the code
under test. The run that caught it started at 2026-11-01T23:59:41Z, finished at
00:02:02Z, and reported `assert '2026-11-02' == '2026-11-01'` on an unrelated
dependency bump. An earlier attempt at the same commit passed.

Two tests here, because either alone is weak. The first asserts no test module reads
the clock at import time, which is the rule. The second shows the rule earns its
keep: it runs a throwaway pytest whose clock steps over midnight between collection
and execution, and checks that the old pattern fails there while the new one holds.
Without it the first test only describes the shape of the code, and the old code
passed 99.8% of the time, which is exactly how a shape-only check reads green while
the race is still live.

Be clear about the division of labour, because the two are easy to conflate. The
second test writes its own throwaway modules, so it demonstrates the mechanism and
will keep demonstrating it forever — but it would still pass if somebody put
`TODAY = dt.date.today()` back into test_forecast.py tomorrow. The first test is the
one that catches that. Neither replaces the other: one proves the rule is real, the
other enforces it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

TESTS_DIR = pathlib.Path(__file__).resolve().parent

# `.today()`, `.now()` and `.utcnow()` on anything. Matched by attribute name rather
# than by resolving `dt`, so `import datetime as d`, `from datetime import date` and
# `dt.datetime.utcnow()` are all caught. An indirection — `TODAY = _helper()` where
# `_helper` reads the clock — is not, which is the honest limit of a syntactic check.
_CLOCK_READS = frozenset({"today", "now", "utcnow"})


def _is_clock_read(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CLOCK_READS
    )


def _import_time_clock_reads(tree: ast.Module) -> list[str]:
    """Clock reads that run when the module is imported, i.e. during collection.

    A function *body* runs when the test runs, which is the whole point, so those are
    skipped. Everything else executes at import: top-level statements, class bodies,
    decorators, and argument defaults — `def _add(..., d: dt.date = dt.date.today())`
    is the same bug wearing a different hat.
    """
    found: list[str] = []

    def walk(node: ast.AST) -> None:
        if _is_clock_read(node):
            found.append(ast.unparse(node))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = node.args
            for evaluated_now in (
                *args.defaults,
                *(d for d in args.kw_defaults if d is not None),
                *getattr(node, "decorator_list", []),
            ):
                walk(evaluated_now)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return found


def _reads_in(source: str) -> list[str]:
    """Convenience for the self-proof below."""
    return _import_time_clock_reads(ast.parse(source))


def test_no_test_module_reads_the_clock_at_import_time() -> None:
    offenders: dict[str, list[str]] = {}
    modules = sorted(TESTS_DIR.glob("*.py"))
    assert len(modules) > 10, "glob found almost nothing — the guard would pass vacuously"

    # The detector is shown to work before its verdict is believed. "No offenders" is
    # otherwise indistinguishable from a matcher that recognises nothing, which is how a
    # vacuous first draft of another guard in this suite once passed. The four positives
    # are the shapes that execute at import; the three negatives are the ones that must
    # not be flagged, because they run when the test runs.
    assert _reads_in("TODAY = dt.date.today()"), "misses the plain module constant"
    assert _reads_in("NOW = datetime.datetime.utcnow()"), "misses utcnow"
    assert _reads_in("T: str = dt.date.today().isoformat()"), "misses the annotated form"
    assert _reads_in(
        "def f(d=dt.date.today()):\n    pass"
    ), "misses an argument default, which is evaluated once at import"
    assert not _reads_in(
        "def f():\n    return dt.date.today()"
    ), "fires inside a function body, where reading the clock is correct"
    assert not _reads_in(
        "def f():\n    d = dt.date.today()\n    return d"
    ), "fires on a local inside a function"
    assert not _reads_in("TODAY = '2026-01-01'"), "fires on a literal"

    for path in modules:
        reads = _import_time_clock_reads(ast.parse(path.read_text(), filename=str(path)))
        if reads:
            offenders[path.name] = reads

    assert not offenders, (
        "These modules read the wall clock while pytest imports them, so the value is "
        "already minutes old by the time the test asserts on it and goes wrong on any "
        "run that crosses midnight. Take the `today` fixture from conftest instead: "
        f"{offenders}"
    )


# A plugin, and two single-test modules, for the subprocess below. The plugin rebinds
# `datetime.date` to a subclass that reads a mutable day, and steps that day forward
# once collection is over — the same interval the real bug lives in. These modules
# touch nothing but `datetime`, which is why rebinding is safe here: doing it to the
# real suite breaks FastAPI's request validation, since `app/deps.py` annotates query
# parameters as `dt.date | None` under `from __future__ import annotations` and
# pydantic resolves those strings lazily, on the first request, picking up the
# substitute class and failing to build a schema for it.
_CLOCK_PLUGIN = """
import datetime as _dt

_REAL_DATE = _dt.date
_BEFORE_MIDNIGHT = _REAL_DATE(2026, 11, 1)
_AFTER_MIDNIGHT = _REAL_DATE(2026, 11, 2)
_clock = {"day": _BEFORE_MIDNIGHT}


class _MovingDate(_REAL_DATE):
    @classmethod
    def today(cls):
        day = _clock["day"]
        return _REAL_DATE(day.year, day.month, day.day)


def pytest_configure(config):
    _dt.date = _MovingDate


def pytest_collection_finish(session):
    # Midnight. Everything imported during collection saw 2026-11-01; everything that
    # runs from here on sees 2026-11-02.
    _clock["day"] = _AFTER_MIDNIGHT


def pytest_unconfigure(config):
    _dt.date = _REAL_DATE
"""

# The pattern the five files used to have.
_AT_IMPORT = """
import datetime as dt

TODAY = dt.date.today()


def test_date_agrees_with_the_app():
    # dt.date.today() stands in for the app, which computes its date per request.
    assert TODAY.isoformat() == dt.date.today().isoformat()
"""

# The pattern they have now: conftest's function-scoped `today` fixture.
_PER_TEST = """
import datetime as dt

import pytest


@pytest.fixture
def today():
    return dt.date.today()


def test_date_agrees_with_the_app(today):
    assert today.isoformat() == dt.date.today().isoformat()
"""


def test_a_run_crossing_midnight_breaks_the_old_pattern_and_not_the_new(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "clock_plugin.py").write_text(_CLOCK_PLUGIN)
    (tmp_path / "test_at_import.py").write_text(_AT_IMPORT)
    (tmp_path / "test_per_test.py").write_text(_PER_TEST)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "clock_plugin",
            "-p",
            "no:cacheprovider",
            "-q",
            "--tb=no",
            "-rf",
            "test_at_import.py",
            "test_per_test.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
    )
    out = proc.stdout + proc.stderr

    context = f"returncode={proc.returncode}\n{out}"

    # Both modules have to have run: a collection error reports ERROR rather than
    # FAILED, so checking the failures alone would let a broken subprocess look right.
    assert "1 failed, 1 passed" in out, (
        f"Expected one test to fail and one to pass under a simulated midnight.\n{context}"
    )
    failed = {line.split()[1] for line in out.splitlines() if line.startswith("FAILED")}
    assert failed == {"test_at_import.py::test_date_agrees_with_the_app"}, (
        "The date captured at import is the one that must go stale when the clock "
        f"crosses midnight mid-run; the per-test read must survive it.\n{context}"
    )
