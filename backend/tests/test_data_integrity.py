"""Bounds and invariants on the numbers and dates the app stores.

Each of these was reachable from an ordinary upload or an ordinary form post, and
each fails quietly rather than loudly: a wrong total, a stretched axis, an import
that never finishes. None would show up as an error in a log.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import create_account
from fastapi.testclient import TestClient

from app.services import dedup, importers


def _txn(client: TestClient, account_id: str, **over: object) -> dict:
    body = {
        "account_id": account_id,
        "txn_date": "2026-03-01",
        "amount_cents": -1250,
        "description": "Coffee",
    }
    body.update(over)
    return client.post("/api/transactions", json=body).json()


# --- Splitting a transfer -------------------------------------------------------


def test_splitting_a_transfer_keeps_the_children_out_of_the_totals(
    auth_client: TestClient,
) -> None:
    """Dashboard totals skip split parents and count their children.

    A child that did not inherit `is_transfer` therefore put money back into income
    and expenses that had only ever moved between the household's own accounts.
    """
    account = create_account(auth_client, "Everyday")
    parent = _txn(
        auth_client, account["id"], amount_cents=-50000, description="Transfer to savings"
    )
    auth_client.patch(f"/api/transactions/{parent['id']}", json={"is_transfer": True})

    children = auth_client.post(
        f"/api/transactions/{parent['id']}/split",
        json={"splits": [{"amount_cents": -30000}, {"amount_cents": -20000}]},
    )
    assert children.status_code == 200, children.text
    assert all(c["is_transfer"] for c in children.json()), (
        "split children dropped the transfer flag, so an internal transfer reappeared "
        "as real spending in every dashboard figure"
    )


# --- Amounts --------------------------------------------------------------------


@pytest.mark.parametrize("cell", ["1e400", "-1e400", "nan", "inf", "1e30"])
def test_an_absurd_amount_in_a_file_does_not_crash_the_import(cell: str) -> None:
    """`round(float("1e400") * 100)` raises OverflowError: a 500 from one cell.

    A merely large finite value is no better — it parses, then overflows the
    column's integer when the transaction is committed.
    """
    cents = importers.to_cents(cell)
    assert isinstance(cents, int)
    assert abs(cents) <= importers.MAX_AMOUNT_CENTS


def test_a_real_amount_still_parses() -> None:
    """The bound must not be doing its job by rejecting everything."""
    assert importers.to_cents("1,234.56") == 123456
    assert importers.to_cents("-45.00") == -4500
    assert importers.to_cents("(12.30)") == -1230


def test_an_absurd_amount_posted_directly_is_refused(auth_client: TestClient) -> None:
    account = create_account(auth_client, "Everyday")
    resp = auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2026-03-01",
            "amount_cents": 10**30,
            "description": "Overflow",
        },
    )
    assert resp.status_code == 422, "an amount past the column's range was accepted"


# --- Dates ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["9999-12-31", "0001-01-01"])
def test_a_date_outside_any_plausible_range_is_refused(
    auth_client: TestClient, bad: str
) -> None:
    """One such row stretched every chart axis and every period the forecast walks."""
    account = create_account(auth_client, "Everyday")
    resp = auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": bad,
            "amount_cents": -100,
            "description": "Far future",
        },
    )
    assert resp.status_code == 422, f"{bad} was accepted as a transaction date"


def test_ordinary_dates_still_work(auth_client: TestClient) -> None:
    account = create_account(auth_client, "Everyday")
    resp = auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2026-03-01",
            "amount_cents": -100,
            "description": "Coffee",
        },
    )
    assert resp.status_code == 201, resp.text


def test_the_file_parser_refuses_the_same_dates() -> None:
    with pytest.raises(ValueError):
        importers.parse_date("31/12/9999")
    assert importers.parse_date("01/03/2026") == dt.date(2026, 3, 1)


# --- The quadratic scans --------------------------------------------------------


def _stored(count: int, *, spread_days: int) -> list[dedup.Candidate]:
    day = dt.date(2026, 1, 1)
    return [
        dedup.Candidate(
            id=f"c{i}",
            txn_date=day + dt.timedelta(days=i % spread_days if spread_days else 0),
            amount_cents=-450,
            raw_description=f"Coffee shop {i}",
            dedup_hash=f"h{i}",
            provider_txn_id=None,
        )
        for i in range(count)
    ]


def _incoming(i: int, *, spread_days: int) -> importers.ParsedTxn:
    day = dt.date(2026, 1, 1)
    return importers.ParsedTxn(
        txn_date=day + dt.timedelta(days=i % spread_days if spread_days else 0),
        amount_cents=-450,
        raw_description=f"Something else entirely {i}",
        merchant="Something Else",
    )


def _run_with_a_comparison_budget(
    matcher: dedup.DuplicateMatcher, rows: int, spread_days: int, budget: int
) -> int:
    """Run an import, aborting the moment the near-match tier exceeds `budget`.

    Aborting rather than asserting at the end is the point: against the unbounded
    version this test would otherwise take minutes to arrive at its own failure.
    """
    comparisons = 0
    real_similarity = dedup.similarity

    def counting(a: str, b: str) -> float:
        nonlocal comparisons
        comparisons += 1
        if comparisons > budget:
            raise AssertionError(
                f"near-match scan passed {budget:,} comparisons for {rows:,} rows"
            )
        return real_similarity(a, b)

    dedup.similarity = counting  # type: ignore[assignment]
    try:
        for i in range(rows):
            matcher.match("acct", _incoming(i, spread_days=spread_days))
    finally:
        dedup.similarity = real_similarity  # type: ignore[assignment]
    return comparisons


def test_duplicate_matching_is_bounded_when_every_row_shares_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adversarial shape: one amount, one date, every row a candidate.

    Bucketing cannot help here — every candidate really is in the window — so the
    per-row cap is the only thing between an upload form and an import that never
    finishes. The cap is lowered for the test so the ratio is visible without doing
    a million comparisons under coverage instrumentation; unbounded, 400 rows
    against 800 stored is 320,000 comparisons and the cap makes it 20,000.
    """
    monkeypatch.setattr(dedup, "MAX_NEAR_MATCH_CANDIDATES", 50)
    matcher = dedup.DuplicateMatcher(_stored(800, spread_days=0))
    rows = 400
    budget = rows * dedup.MAX_NEAR_MATCH_CANDIDATES + 500
    _run_with_a_comparison_budget(matcher, rows, spread_days=0, budget=budget)


class _CountingCandidate(dedup.Candidate):
    """A candidate that records every time the scan looks at it.

    Comparison count is the wrong measure for this one: the old code filtered by
    date *before* comparing, so it made the same number of comparisons while
    walking every stored row to get there. What changed is how many rows it has to
    touch at all, and `consumed` is read exactly once per row the scan considers.
    """

    touches = 0

    def __getattribute__(self, name: str) -> object:
        if name == "consumed":
            _CountingCandidate.touches += 1
        return object.__getattribute__(self, name)


def test_duplicate_matching_only_looks_inside_the_date_window() -> None:
    """A year of history: only the few days either side can ever match.

    Bucketed on amount alone, every incoming row walked all 1,000 stored rows to
    reach the handful in range — 300 x 1,000 = 300,000 rows touched, against the
    ~6,000 the date window actually allows.
    """
    count = 1000
    stored = [
        _CountingCandidate(
            id=c.id,
            txn_date=c.txn_date,
            amount_cents=c.amount_cents,
            raw_description=c.raw_description,
            dedup_hash=c.dedup_hash,
            provider_txn_id=c.provider_txn_id,
        )
        for c in _stored(count, spread_days=365)
    ]
    matcher = dedup.DuplicateMatcher(stored)
    _CountingCandidate.touches = 0
    rows = 300
    for i in range(rows):
        matcher.match("acct", _incoming(i, spread_days=365))

    window = 2 * dedup.DATE_WINDOW_DAYS + 1
    ceiling = rows * (count // 365 + 2) * window
    assert _CountingCandidate.touches <= ceiling, (
        f"scan touched {_CountingCandidate.touches:,} stored rows for {rows:,} incoming "
        f"rows; the date window allows at most {ceiling:,}"
    )


def test_duplicate_matching_still_finds_a_near_match() -> None:
    """The narrowing must not have narrowed the answer away."""
    stored = [
        dedup.Candidate(
            id="c1",
            txn_date=dt.date(2026, 3, 2),
            amount_cents=-450,
            raw_description="COFFEE SHOP SYDNEY 1234",
            dedup_hash="h1",
            provider_txn_id=None,
        )
    ]
    matcher = dedup.DuplicateMatcher(stored)
    verdict = matcher.match(
        "acct",
        importers.ParsedTxn(
            txn_date=dt.date(2026, 3, 1),
            amount_cents=-450,
            raw_description="COFFEE SHOP SYDNEY 9876",
            merchant="Coffee Shop",
        ),
    )
    assert verdict.status == dedup.DUPLICATE_PROBABLE, verdict.status


def test_transfer_detection_only_pairs_inside_its_window(auth_client: TestClient) -> None:
    """Equal and opposite rows in two accounts: the worst case for pairing.

    Grouped on amount alone, every outflow was compared against every same-amount
    inflow in the whole scoped set, to find the handful within three days. Counted
    rather than timed, so the bound is the same on a loaded CI runner as it is here.
    """
    from app import models
    from app.db import SessionLocal
    from app.services import transfers

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    a = create_account(auth_client, "A")
    b = create_account(auth_client, "B")
    day = dt.date(2026, 1, 1)

    with SessionLocal() as db:
        for i in range(200):
            when = day + dt.timedelta(days=i % 60)
            for account_id, cents, text in (
                (a["id"], -2500, "Groceries"),
                (b["id"], 2500, "Refund"),
            ):
                db.add(
                    models.Transaction(
                        household_id=household_id,
                        account_id=account_id,
                        txn_date=when,
                        amount_cents=cents,
                        raw_description=f"{text} {i}",
                        merchant=text,
                        source="import",
                        dedup_hash=f"{account_id}-{i}-{cents}",
                    )
                )
        db.commit()

    # Counting what the scan considers, not what it compares: the date filter used to
    # run *after* the walk, so the number of comparisons was already small while the
    # walk itself was quadratic.
    considered = 0
    real = transfers.candidates

    def counting(*args: object, **kwargs: object) -> list:
        nonlocal considered
        found = real(*args, **kwargs)  # type: ignore[arg-type]
        considered += len(found)
        return found

    transfers.candidates = counting  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            transfers.detect_transfers(db, household_id)
    finally:
        transfers.candidates = real  # type: ignore[assignment]

    # 200 outflows over 60 days is ~3 inflows a day; a 7-day window allows ~21 each.
    # Grouped on amount alone every outflow saw all 200, which is 40,000.
    ceiling = 200 * (200 // 60 + 2) * (2 * 3 + 1)
    assert considered <= ceiling, (
        f"transfer matching considered {considered:,} inflows; the window allows {ceiling:,}"
    )


# --- Malformed CSV, found by the fuzzer -----------------------------------------


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("bare-carriage-return", b"Date,Amount\n2026-01-01,1.00\n\r0,2.00\n"),
        ("oversized-field", b"Date,Description,Amount\n2026-01-01," + b"x" * 200_000 + b",1.00\n"),
    ],
    ids=["bare-carriage-return", "oversized-field"],
)
def test_a_csv_the_parser_refuses_comes_back_as_a_message(
    auth_client: TestClient, name: str, content: bytes
) -> None:
    """`_csv.Error` is not a `ValueError`, so it sailed past the API's handler.

    A bare carriage return inside an unquoted field is something banks actually
    emit, and it reached the user as a 500 with nothing to act on.
    """
    create_account(auth_client, "Everyday")
    resp = auth_client.post(
        "/api/imports/sniff", files={"file": (f"{name}.csv", content, "text/csv")}
    )
    assert resp.status_code < 500, f"{name} produced a {resp.status_code}: {resp.text[:200]}"
