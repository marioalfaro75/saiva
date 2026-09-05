"""Data quality across a *sequence* of imports, which is how people actually import.

The existing dedup tests ask "does this pair get matched". These ask the question a
household actually cares about: after months of exports — overlapping, in different
formats, re-exported after the bank re-words things, with edits and splits in between
— does the ledger hold exactly one row per real transaction, and do the numbers on the
dashboard stay put?

They are written as invariants rather than examples, because the failure mode here is
never a crash. It is a balance that drifts by $80 and nobody notices for a year.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import itertools
import json
import random
from dataclasses import dataclass

import pytest
from conftest import create_account
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class Txn:
    """One real-world transaction, independent of how a bank chooses to render it."""

    day: dt.date
    cents: int  # negative = money out
    text: str


def csv_debit_credit(txns: list[Txn], *, date_fmt: str = "%d/%m/%Y") -> bytes:
    """The Westpac-style shape: separate Debit and Credit columns, unsigned."""
    out = io.StringIO()
    out.write("Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance\n")
    for t in txns:
        debit = f"{abs(t.cents) / 100:.2f}" if t.cents < 0 else ""
        credit = f"{t.cents / 100:.2f}" if t.cents > 0 else ""
        out.write(f"123-456 789,{t.day.strftime(date_fmt)},{t.text},{debit},{credit},0.00\n")
    return out.getvalue().encode()


def csv_signed(txns: list[Txn], *, date_fmt: str = "%Y-%m-%d") -> bytes:
    """The other common shape: one signed Amount column, different header names."""
    out = io.StringIO()
    out.write("Date,Description,Amount\n")
    for t in txns:
        out.write(f"{t.day.strftime(date_fmt)},{t.text},{t.cents / 100:.2f}\n")
    return out.getvalue().encode()


def ofx(txns: list[Txn], *, with_fitid: bool = True) -> bytes:
    """An OFX statement for one account.

    The FITID is derived from the transaction itself plus how many times that exact
    transaction has already appeared in this statement — never from its row number.
    A real bank's FITID is stable for a transaction across every export and unique
    per occurrence, and a generator that numbers them per file instead hands two
    different transactions the same id in two different exports. That is not a
    finding about the importer; it is a broken fixture that manufactures one.
    """
    body = []
    seen: dict[tuple[dt.date, int, str], int] = {}
    for t in txns:
        key = (t.day, t.cents, t.text)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        digest = hashlib.sha1(
            f"{t.day}|{t.cents}|{t.text}|{occurrence}".encode()
        ).hexdigest()[:12]
        fit = f"<FITID>{digest}" if with_fitid else ""
        body.append(
            f"<STMTTRN><TRNTYPE>{'DEBIT' if t.cents < 0 else 'CREDIT'}"
            f"<DTPOSTED>{t.day.strftime('%Y%m%d')}<TRNAMT>{t.cents / 100:.2f}{fit}"
            f"<NAME>{t.text}</NAME></STMTTRN>"
        )
    return (
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><ACCTID>123456789</ACCTID>"
        "</BANKACCTFROM><BANKTRANLIST>" + "".join(body) + "</BANKTRANLIST></STMTRS>"
        "</STMTTRNRS></BANKMSGSRSV1></OFX>"
    ).encode()


def commit(
    client: TestClient,
    content: bytes,
    *,
    account_id: str | None = None,
    fmt: str = "csv",
    name: str = "export",
    assignments: list[dict] | None = None,
    account_col: int | None = None,
) -> dict:
    """Import a file the way the UI does: sniff for a mapping, then commit."""
    files = {"file": (f"{name}.{fmt}", content, "application/octet-stream")}
    data: dict[str, str] = {"file_format": fmt}
    if account_id:
        data["account_id"] = account_id
    if fmt == "csv":
        sniffed = client.post(
            "/api/imports/sniff", files={"file": (f"{name}.csv", content, "text/csv")}
        )
        assert sniffed.status_code == 200, sniffed.text
        mapping = sniffed.json()["suggested_mapping"]
        assert mapping is not None, "the sniffer could not map this file"
        if account_col is not None:
            mapping["account_col"] = account_col
        data["mapping"] = json.dumps(mapping)
    if assignments is not None:
        data["assignments"] = json.dumps(assignments)
    resp = client.post("/api/imports/commit", files=files, data=data)
    assert resp.status_code == 200, resp.text
    return resp.json()


def preview(client: TestClient, content: bytes, *, account_id: str, fmt: str = "csv") -> dict:
    files = {"file": (f"p.{fmt}", content, "application/octet-stream")}
    data: dict[str, str] = {"file_format": fmt, "account_id": account_id}
    if fmt == "csv":
        sniffed = client.post("/api/imports/sniff", files={"file": ("p.csv", content, "text/csv")})
        data["mapping"] = json.dumps(sniffed.json()["suggested_mapping"])
    resp = client.post("/api/imports/preview", files=files, data=data)
    assert resp.status_code == 200, resp.text
    return resp.json()


PAGE_SIZE = 200  # the API's maximum


def ledger(client: TestClient) -> list[tuple[str, int, str]]:
    """Every stored transaction, in a stable comparable form.

    Paged rather than asking for one big page: the API caps page_size, and a helper
    that quietly returned the first 200 rows would make every count in this file a
    lie in exactly the direction the tests are meant to catch.
    """
    rows: list[tuple[str, int, str]] = []
    page = 1
    while True:
        resp = client.get(f"/api/transactions?page={page}&page_size={PAGE_SIZE}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows += [
            (t["txn_date"], t["amount_cents"], (t["raw_description"] or "").strip())
            for t in body["items"]
        ]
        if len(rows) >= body["total"] or not body["items"]:
            assert len(rows) == body["total"], f"paged {len(rows)} of {body['total']}"
            return sorted(rows)
        page += 1


def balance(client: TestClient) -> int:
    return sum(cents for _, cents, _ in ledger(client))


# A month of ordinary household activity.
MARCH = [
    Txn(dt.date(2026, 3, 1), -45_50, "WOOLWORTHS METRO"),
    Txn(dt.date(2026, 3, 3), -12_00, "COFFEE CLUB"),
    Txn(dt.date(2026, 3, 5), -1000_00, "RENT PAYMENT"),
    Txn(dt.date(2026, 3, 12), 3200_00, "SALARY ACME PTY LTD"),
    Txn(dt.date(2026, 3, 18), -87_65, "COLES SUPERMARKET"),
    Txn(dt.date(2026, 3, 25), -60_00, "AMPOL SERVICE STATION"),
]
APRIL = [
    Txn(dt.date(2026, 4, 2), -52_10, "WOOLWORTHS METRO"),
    Txn(dt.date(2026, 4, 5), -1000_00, "RENT PAYMENT"),
    Txn(dt.date(2026, 4, 12), 3200_00, "SALARY ACME PTY LTD"),
]


@pytest.fixture
def account(auth_client: TestClient) -> str:
    return create_account(auth_client, "Everyday")["id"]


# ------------------------------------------------------------------ 1. conservation


def test_every_row_is_either_added_or_skipped_never_lost(
    auth_client: TestClient, account: str
) -> None:
    """`added + skipped` must account for the whole file, every time.

    A row that is neither is a row the household believes it imported and did not.
    Nothing in the response would tell them.
    """
    for _ in range(3):
        result = commit(auth_client, csv_debit_credit(MARCH), account_id=account)
        assert result["added"] + result["skipped"] == len(MARCH), result


def test_the_ledger_grows_by_exactly_what_was_added(
    auth_client: TestClient, account: str
) -> None:
    before = len(ledger(auth_client))
    result = commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    assert len(ledger(auth_client)) == before + result["added"]

    before = len(ledger(auth_client))
    result = commit(auth_client, csv_debit_credit(MARCH + APRIL), account_id=account)
    assert len(ledger(auth_client)) == before + result["added"]


def test_no_imported_row_is_orphaned_from_its_batch(
    auth_client: TestClient, account: str
) -> None:
    """Every imported row must be traceable to the file it came from."""
    from app import models
    from app.db import SessionLocal

    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    with SessionLocal() as db:
        orphans = [
            t.raw_description
            for t in db.query(models.Transaction).filter_by(source="import").all()
            if t.import_batch_id is None
        ]
    assert orphans == [], f"imported rows with no batch: {orphans}"


# ------------------------------------------------------------------- 2. convergence


def test_importing_the_same_file_repeatedly_changes_nothing(
    auth_client: TestClient, account: str
) -> None:
    """The property a household actually relies on: re-importing is a no-op.

    Asserted on the ledger and the balance, not on the skip count — a skip count can
    be right while the stored rows are wrong.
    """
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    settled, settled_balance = ledger(auth_client), balance(auth_client)

    for _ in range(5):
        commit(auth_client, csv_debit_credit(MARCH), account_id=account)
        assert ledger(auth_client) == settled
        assert balance(auth_client) == settled_balance


def test_the_dashboard_total_does_not_move_on_re_import(
    auth_client: TestClient, account: str
) -> None:
    """The number on the screen is the thing a duplicate actually corrupts."""
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    first = auth_client.get("/api/dashboard/summary?period=custom"
                            "&start=2026-03-01&end=2026-03-31")
    assert first.status_code == 200, first.text

    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    again = auth_client.get("/api/dashboard/summary?period=custom"
                            "&start=2026-03-01&end=2026-03-31")
    assert again.json() == first.json(), "re-importing moved the dashboard figures"


# ----------------------------------------------------------------------- 3. overlap


def test_three_overlapping_monthly_exports_produce_their_union(
    auth_client: TestClient, account: str
) -> None:
    """How people really export: this month, then a range that covers last month too."""
    jan = [Txn(dt.date(2026, 1, 5), -1000_00, "RENT"), Txn(dt.date(2026, 1, 20), -80_00, "IGA")]
    feb = [Txn(dt.date(2026, 2, 5), -1000_00, "RENT"), Txn(dt.date(2026, 2, 18), -60_00, "SHELL")]
    for chunk in (jan, jan + feb, feb):
        commit(auth_client, csv_debit_credit(chunk), account_id=account)
    assert len(ledger(auth_client)) == 4


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_the_result_does_not_depend_on_the_order_files_are_imported(
    auth_client: TestClient, account: str, order: tuple[int, ...]
) -> None:
    """Whichever order the exports arrive in, the ledger must end up the same.

    Order dependence is how one household ends up with 41 rows and another with 43
    from the same three files, and neither can tell which is right.
    """
    chunks = [MARCH[:3], MARCH[2:5], MARCH[4:]]
    for i in order:
        commit(auth_client, csv_debit_credit(chunks[i]), account_id=account)
    assert ledger(auth_client) == sorted(
        (t.day.isoformat(), t.cents, t.text) for t in MARCH
    )


# ------------------------------------------------------------ 4. format independence


def test_the_same_month_as_ofx_then_csv_imports_once(
    auth_client: TestClient, account: str
) -> None:
    """Banks offer both, and people download both."""
    commit(auth_client, ofx(MARCH), account_id=account, fmt="ofx")
    settled = ledger(auth_client)
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    assert ledger(auth_client) == settled


def test_the_same_month_as_csv_then_ofx_imports_once(
    auth_client: TestClient, account: str
) -> None:
    """The reverse order matters too: CSV rows carry no FITID for OFX to match on."""
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    settled = ledger(auth_client)
    commit(auth_client, ofx(MARCH), account_id=account, fmt="ofx")
    assert ledger(auth_client) == settled


def test_a_bank_wording_the_same_transaction_differently_does_not_duplicate_it(
    auth_client: TestClient, account: str
) -> None:
    """The case that made this suite worth writing.

    A bank renders one purchase as "WOOLWORTHS METRO" in OFX and
    "EFTPOS WOOLWORTHS 4521 SYDNEY NSW" in CSV. Those score far below the description
    similarity threshold, so before the merchant tier existed, importing both formats
    of one month produced two rows for every transaction in it.
    """
    commit(auth_client, ofx([Txn(dt.date(2026, 3, 1), -45_50, "WOOLWORTHS METRO")]),
           account_id=account, fmt="ofx")
    reworded = [Txn(dt.date(2026, 3, 1), -45_50, "EFTPOS WOOLWORTHS 4521 SYDNEY NSW")]
    commit(auth_client, csv_debit_credit(reworded), account_id=account)
    assert len(ledger(auth_client)) == 1, ledger(auth_client)


def test_two_different_shops_charging_the_same_amount_are_both_kept(
    auth_client: TestClient, account: str
) -> None:
    """The merchant tier must not have bought its win by over-matching.

    $20.00 at Coles and $20.00 at Bunnings on one Saturday is two real purchases.
    They arrive in *separate* imports on purpose: rows inside one file are never
    matched against each other, so putting both in one file tests nothing. An
    earlier version of this test did exactly that and passed happily against a
    deduper that collapsed any two same-amount, same-day rows into one.
    """
    coles = [Txn(dt.date(2026, 3, 7), -20_00, "COLES SUPERMARKET")]
    bunnings = [Txn(dt.date(2026, 3, 7), -20_00, "BUNNINGS WAREHOUSE")]
    commit(auth_client, csv_debit_credit(coles), account_id=account)
    result = commit(auth_client, csv_debit_credit(bunnings), account_id=account)
    assert result["added"] == 1, "a different shop was mistaken for one already stored"
    assert len(ledger(auth_client)) == 2


def test_a_later_export_repeating_one_shop_still_adds_the_other(
    auth_client: TestClient, account: str
) -> None:
    """The overlap version: the second export repeats Coles and introduces Bunnings."""
    coles = Txn(dt.date(2026, 3, 7), -20_00, "COLES SUPERMARKET")
    bunnings = Txn(dt.date(2026, 3, 7), -20_00, "BUNNINGS WAREHOUSE")
    commit(auth_client, csv_debit_credit([coles]), account_id=account)
    result = commit(auth_client, csv_debit_credit([coles, bunnings]), account_id=account)
    assert (result["added"], result["skipped"]) == (1, 1), result
    assert len(ledger(auth_client)) == 2


@pytest.mark.parametrize(
    ("name", "second"),
    [
        ("iso-dates", lambda t: csv_debit_credit(t, date_fmt="%Y-%m-%d")),
        ("signed-amount-column", csv_signed),
        ("ofx-without-fitid", lambda t: ofx(t, with_fitid=False)),
    ],
)
def test_the_same_data_in_a_different_shape_still_deduplicates(
    auth_client: TestClient, account: str, name: str, second
) -> None:
    """Banks change their export format. The transactions did not change."""
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    settled = ledger(auth_client)
    content = second(MARCH)
    commit(auth_client, content, account_id=account,
           fmt="ofx" if content.lstrip().startswith(b"<OFX") else "csv")
    assert ledger(auth_client) == settled, f"{name} re-imported rows that already existed"


def test_whitespace_and_case_drift_is_not_a_new_transaction(
    auth_client: TestClient, account: str
) -> None:
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    settled = ledger(auth_client)
    noisy = [Txn(t.day, t.cents, f"  {t.text.lower()}   ") for t in MARCH]
    commit(auth_client, csv_debit_credit(noisy), account_id=account)
    assert ledger(auth_client) == settled


# ------------------------------------------------------------------ 5. multiplicity


def test_genuine_same_day_repeats_survive_repeated_importing(
    auth_client: TestClient, account: str
) -> None:
    """Two identical coffees on one day are two real events, not a duplicate.

    Deduplication that cannot tell them apart quietly deletes real spending, which is
    the failure that looks most like the app working correctly.
    """
    coffees = [Txn(dt.date(2026, 3, 3), -4_50, "COFFEE CLUB")] * 3
    for _ in range(4):
        commit(auth_client, csv_debit_credit(coffees), account_id=account)
    assert len(ledger(auth_client)) == 3


def test_a_later_export_containing_one_more_repeat_adds_only_that_one(
    auth_client: TestClient, account: str
) -> None:
    two = [Txn(dt.date(2026, 3, 3), -4_50, "COFFEE CLUB")] * 2
    three = [Txn(dt.date(2026, 3, 3), -4_50, "COFFEE CLUB")] * 3
    commit(auth_client, csv_debit_credit(two), account_id=account)
    result = commit(auth_client, csv_debit_credit(three), account_id=account)
    assert (result["added"], result["skipped"]) == (1, 2)
    assert len(ledger(auth_client)) == 3


# --------------------------------------------------- 6. edits must not break dedup


def test_recategorising_and_flagging_a_transfer_does_not_unmask_a_duplicate(
    auth_client: TestClient, account: str
) -> None:
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    rows = auth_client.get("/api/transactions?page_size=50").json()["items"]
    categories = auth_client.get("/api/categories").json()
    category_id = (categories if isinstance(categories, list) else categories["items"])[0]["id"]
    auth_client.post(f"/api/transactions/{rows[0]['id']}/recategorise",
                     json={"category_id": category_id, "scope": "none"})
    auth_client.patch(f"/api/transactions/{rows[1]['id']}", json={"is_transfer": True})

    before = len(ledger(auth_client))
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    assert len(ledger(auth_client)) == before


def test_splitting_a_transaction_does_not_let_the_original_re_import(
    auth_client: TestClient, account: str
) -> None:
    """A split adds child rows of its own; the parent must still match the file.

    Otherwise the next export re-adds the parent and the household is charged twice
    for one bill — once as the split, once as the reimported original.
    """
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    rent = next(t for t in auth_client.get("/api/transactions?page_size=50").json()["items"]
                if t["amount_cents"] == -1000_00)
    split = auth_client.post(f"/api/transactions/{rent['id']}/split",
                             json={"splits": [{"amount_cents": -600_00},
                                              {"amount_cents": -400_00}]})
    assert split.status_code == 200, split.text

    before = len(ledger(auth_client))
    result = commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    assert result["added"] == 0, "the split parent was re-imported as a new transaction"
    assert len(ledger(auth_client)) == before


def test_a_deleted_transaction_comes_back_on_re_import(
    auth_client: TestClient, account: str
) -> None:
    """Pinning the behaviour rather than endorsing it.

    Deleting a row removes the thing dedup matches against, so the next export
    restores it. That is defensible — the transaction did happen — but it is
    surprising, and a change here should be a decision rather than an accident.
    """
    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    rows = auth_client.get("/api/transactions?page_size=50").json()["items"]
    auth_client.delete(f"/api/transactions/{rows[0]['id']}")
    assert len(ledger(auth_client)) == len(MARCH) - 1

    commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    assert len(ledger(auth_client)) == len(MARCH)


# ------------------------------------------------------------------ 7. many accounts


def test_the_same_description_in_two_accounts_is_two_transactions(
    auth_client: TestClient, account: str
) -> None:
    """Dedup is per account. A $1,000 rent payment from each of two accounts is two."""
    other = create_account(auth_client, "Offset", "savings")["id"]
    one = [Txn(dt.date(2026, 3, 5), -1000_00, "RENT PAYMENT")]
    commit(auth_client, csv_debit_credit(one), account_id=account)
    commit(auth_client, csv_debit_credit(one), account_id=other)
    assert len(ledger(auth_client)) == 2


def test_a_multi_account_file_imported_twice_stays_deduplicated(
    auth_client: TestClient, account: str
) -> None:
    """One file covering several accounts is the normal Westpac export."""
    other = create_account(auth_client, "Savings", "savings")["id"]
    content = (
        b"Bank Account,Date,Narrative,Debit Amount,Credit Amount\n"
        b"111-222 333444,05/03/2026,RENT PAYMENT,1000.00,\n"
        b"555-666 777888,06/03/2026,IGA SUPERMARKET,80.00,\n"
    )
    sniffed = auth_client.post(
        "/api/imports/sniff", files={"file": ("m.csv", content, "text/csv")}
    ).json()
    mapping = sniffed["suggested_mapping"]
    mapping["account_col"] = 0
    scan = auth_client.post(
        "/api/imports/accounts/scan",
        files={"file": ("m.csv", content, "text/csv")},
        data={"file_format": "csv", "mapping": json.dumps(mapping)},
    )
    assert scan.status_code == 200, scan.text
    assignments = [
        {"value": row["value"], "account_id": target}
        for row, target in zip(scan.json(), (account, other), strict=True)
    ]

    first = commit(auth_client, content, assignments=assignments, account_col=0)
    assert first["added"] == 2, first
    second = commit(auth_client, content, assignments=assignments, account_col=0)
    assert (second["added"], second["skipped"]) == (0, 2), second
    assert len(ledger(auth_client)) == 2


# -------------------------------------------------------------- 8. preview honesty


def test_the_preview_promises_what_the_commit_delivers(
    auth_client: TestClient, account: str
) -> None:
    """A preview that disagrees with the commit is worse than no preview."""
    commit(auth_client, csv_debit_credit(MARCH[:3]), account_id=account)

    shown = preview(auth_client, csv_debit_credit(MARCH), account_id=account)
    expected_new = sum(1 for row in shown["rows"] if row["status"] == "new")

    result = commit(auth_client, csv_debit_credit(MARCH), account_id=account)
    assert result["added"] == expected_new, (
        f"preview showed {expected_new} new rows, commit added {result['added']}"
    )


# ------------------------------------------------------------------- 9. the hash


def test_the_dedup_hash_is_stable_and_field_sensitive() -> None:
    """It is the identity of a transaction; it must change if and only if one does."""
    from app.services.importers import dedup_hash

    base = ("acct-1", dt.date(2026, 3, 1), -4550, "WOOLWORTHS METRO")
    assert dedup_hash(*base) == dedup_hash(*base)
    # Cosmetic differences are the same transaction.
    assert dedup_hash(*base) == dedup_hash("acct-1", dt.date(2026, 3, 1), -4550,
                                           "  woolworths   METRO ")
    # Any real difference is a different transaction — including the account.
    variants = [
        ("acct-2", dt.date(2026, 3, 1), -4550, "WOOLWORTHS METRO"),
        ("acct-1", dt.date(2026, 3, 2), -4550, "WOOLWORTHS METRO"),
        ("acct-1", dt.date(2026, 3, 1), -4551, "WOOLWORTHS METRO"),
        ("acct-1", dt.date(2026, 3, 1), 4550, "WOOLWORTHS METRO"),
        ("acct-1", dt.date(2026, 3, 1), -4550, "COLES SUPERMARKET"),
    ]
    for variant in variants:
        assert dedup_hash(*base) != dedup_hash(*variant), variant


# ------------------------------------------- 10. the whole thing, on random inputs


@pytest.mark.parametrize("seed", range(6))
def test_a_year_of_overlapping_exports_reconstructs_the_ledger_exactly(
    auth_client: TestClient, account: str, seed: int
) -> None:
    """The composition of everything above, on data no one hand-picked.

    Build a year of transactions, slice it into overlapping exports in a random
    order and a random mix of formats — the way a real person accumulates them —
    and require the ledger at the end to equal the source exactly. Every duplicate
    shows up as an extra row; every over-match shows up as a missing one.
    """
    rng = random.Random(seed)
    merchants = ["WOOLWORTHS METRO", "COLES SUPERMARKET", "AMPOL", "COFFEE CLUB",
                 "RENT PAYMENT", "SALARY ACME PTY LTD", "BUNNINGS", "NETFLIX"]
    truth: list[Txn] = []
    for day_offset in range(0, 360, 3):
        day = dt.date(2026, 1, 1) + dt.timedelta(days=day_offset)
        for _ in range(rng.randint(1, 3)):
            text = rng.choice(merchants)
            cents = rng.choice([-1, 1]) * rng.randint(1, 400_000)
            truth.append(Txn(day, cents, text))

    # Overlapping windows, like exporting "the last two months" every month.
    windows = []
    for start in range(0, len(truth), 12):
        windows.append(truth[max(0, start - 6): start + 18])
    rng.shuffle(windows)

    shapes = [
        lambda t: (csv_debit_credit(t), "csv"),
        lambda t: (csv_debit_credit(t, date_fmt="%Y-%m-%d"), "csv"),
        lambda t: (csv_signed(t), "csv"),
        lambda t: (ofx(t), "ofx"),
    ]
    for window in windows:
        if not window:
            continue
        content, fmt = rng.choice(shapes)(window)
        commit(auth_client, content, account_id=account, fmt=fmt)

    expected = sorted((t.day.isoformat(), t.cents, t.text) for t in truth)
    actual = ledger(auth_client)
    assert len(actual) == len(expected), (
        f"seed {seed}: expected {len(expected)} transactions, ledger holds {len(actual)}"
    )
    assert actual == expected
