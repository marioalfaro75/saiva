"""The payload a reviewer decides from, and the cost of producing it.

A "possible duplicate" asks a person a question, and until now the answer lived in a
hover tooltip that does not exist on a phone. These tests pin what the preview tells
them instead — and, because the answer is assembled per row, that asking costs no
more queries for fifty matches than for one.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import create_account
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.services import dedup as dedup_module

HEADER = b"Date,Narrative,Debit Amount,Credit Amount\n"


def csv_rows(*rows: tuple[str, str, str]) -> bytes:
    body = b"".join(f"{d},{text},{amount},\n".encode() for d, text, amount in rows)
    return HEADER + body


def send(client: TestClient, content: bytes, account_id: str, *, commit: bool) -> dict:
    files = {"file": ("export.csv", content, "text/csv")}
    sniffed = client.post(
        "/api/imports/sniff", files={"file": ("export.csv", content, "text/csv")}
    ).json()
    data = {
        "account_id": account_id,
        "file_format": "csv",
        "mapping": json.dumps(sniffed["suggested_mapping"]),
    }
    endpoint = "/api/imports/commit" if commit else "/api/imports/preview"
    resp = client.post(endpoint, files=files, data=data)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def account(auth_client: TestClient) -> str:
    return create_account(auth_client, "Everyday")["id"]


def only_row(preview: dict) -> dict:
    assert len(preview["rows"]) == 1, preview["rows"]
    return preview["rows"][0]


# ------------------------------------------------------------------- per tier


def test_a_near_match_reports_the_stored_row_and_which_way_the_date_moved(
    auth_client: TestClient, account: str
) -> None:
    """The matcher writes its reason with abs(), which loses the direction.

    A stored row a couple of days *earlier* is what a bank re-dating a pending
    authorisation looks like; one *later* is more likely a genuine second purchase.
    Same two days either way in the prose, opposite conclusions.
    """
    send(auth_client, csv_rows(("01/06/2026", "COFFEE CLUB 1234", "12.00")),
         account, commit=True)
    row = only_row(
        send(auth_client, csv_rows(("03/06/2026", "COFFEE CLUB 9876", "12.00")),
             account, commit=False)
    )
    assert row["status"] == "duplicate_probable"
    assert row["match_tier"] == "near_match"
    # Negative: the stored row is earlier than the incoming one.
    assert row["matched_days_delta"] == -2
    assert row["matched_amount_cents"] == -1200
    assert row["matched_description"] == "COFFEE CLUB 1234"
    assert row["matched_source"] == "import"
    assert row["matched_imported_from"] == "export.csv"
    # The prose the preview already relied on must survive unchanged.
    assert "2 days apart" in row["duplicate_reason"]
    assert row["matched_date"] == "2026-06-01"


def test_a_reference_number_that_changed_is_not_called_harmless(
    auth_client: TestClient, account: str
) -> None:
    """`fuzzy_norm` strips runs of four or more digits, so two transfers to
    *different* payees normalise equal — which is what rent and a loan repayment
    look like. Reporting that as "only a reference number" is the one thing here
    that could talk someone into losing a real transaction."""
    send(auth_client, csv_rows(("01/06/2026", "TRANSFER TO 123456789", "500.00")),
         account, commit=True)
    row = only_row(
        send(auth_client, csv_rows(("02/06/2026", "TRANSFER TO 987654321", "500.00")),
             account, commit=False)
    )
    assert row["matched_wording"] == "digits"


def test_the_same_reference_number_is_reported_as_noise(
    auth_client: TestClient, account: str
) -> None:
    send(auth_client, csv_rows(("01/06/2026", "WOOLWORTHS 1234 SYDNEY", "52.10")),
         account, commit=True)
    row = only_row(
        send(auth_client, csv_rows(("02/06/2026", "WOOLWORTHS 1234 SYDNEY.", "52.10")),
             account, commit=False)
    )
    assert row["matched_wording"] == "noise"


def test_the_merchant_tier_carries_the_differently_worded_stored_row(
    auth_client: TestClient, account: str
) -> None:
    """The OFX-versus-CSV case: one purchase, two wordings, both needed on screen."""
    send(auth_client, csv_rows(("01/06/2026", "WOOLWORTHS METRO", "45.50")),
         account, commit=True)
    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "EFTPOS WOOLWORTHS 4521 SYDNEY NSW", "45.50")),
             account, commit=False)
    )
    assert row["match_tier"] == "same_merchant"
    assert row["matched_description"] == "WOOLWORTHS METRO"
    assert row["matched_wording"] == "differs"
    assert row["matched_merchant"] == "Woolworths"


def test_a_hand_entered_match_says_so(auth_client: TestClient, account: str) -> None:
    """The case that most deserves a second look, and the one the app could not
    previously express at all."""
    created = auth_client.post(
        "/api/transactions",
        json={
            "account_id": account,
            "txn_date": "2026-06-01",
            "amount_cents": -8420,
            "description": "WOOLWORTHS METRO",
        },
    )
    assert created.status_code == 201, created.text

    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "EFTPOS WOOLWORTHS 4521 NSW", "84.20")),
             account, commit=False)
    )
    assert row["matched_source"] == "manual"
    assert row["matched_imported_from"] is None


def test_the_stored_rows_own_category_is_reported(
    auth_client: TestClient, account: str
) -> None:
    """Not the categoriser's guess for the incoming row — the real one, which the
    household may have corrected by hand."""
    send(auth_client, csv_rows(("01/06/2026", "WOOLWORTHS METRO", "45.50")),
         account, commit=True)
    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "EFTPOS WOOLWORTHS 4521 NSW", "45.50")),
             account, commit=False)
    )
    assert row["matched_category_name"]


# --------------------------------------------------------------- neighbourhood


def test_the_neighbourhood_counts_what_the_account_already_holds(
    auth_client: TestClient, account: str
) -> None:
    """One candidate in isolation answers "does something like this exist" and not
    "how many of these do I have" — and the second is what separates a re-dated
    duplicate from a weekly repeat."""
    send(
        auth_client,
        csv_rows(
            ("01/06/2026", "WOOLWORTHS METRO", "45.50"),
            ("03/06/2026", "BUNNINGS WAREHOUSE", "45.50"),
            ("08/06/2026", "WOOLWORTHS METRO SURRY HILLS", "45.50"),
        ),
        account,
        commit=True,
    )
    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "EFTPOS WOOLWORTHS 4521 NSW", "45.50")),
             account, commit=False)
    )
    # Two others, and never the matched row itself.
    assert row["matched_neighbours_total"] == 2
    assert row["matched_txn_id"] not in {n.get("id") for n in row["matched_neighbours"]}
    descriptions = {n["description"] for n in row["matched_neighbours"]}
    assert descriptions == {"BUNNINGS WAREHOUSE", "WOOLWORTHS METRO SURRY HILLS"}
    # Each neighbour says why it is listed, so the copy cannot over-claim.
    by_description = {n["description"]: n for n in row["matched_neighbours"]}
    assert by_description["BUNNINGS WAREHOUSE"]["same_amount"] is True
    assert by_description["BUNNINGS WAREHOUSE"]["same_merchant"] is False
    assert by_description["WOOLWORTHS METRO SURRY HILLS"]["same_merchant"] is True


def test_the_neighbourhood_is_empty_when_the_account_holds_nothing_else(
    auth_client: TestClient, account: str
) -> None:
    send(auth_client, csv_rows(("01/06/2026", "WOOLWORTHS METRO", "45.50")),
         account, commit=True)
    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "EFTPOS WOOLWORTHS 4521 NSW", "45.50")),
             account, commit=False)
    )
    assert row["matched_neighbours_total"] == 0
    assert row["matched_neighbours"] == []


def test_rows_that_need_no_decision_carry_no_neighbourhood(
    auth_client: TestClient, account: str
) -> None:
    """It is evidence for a judgement. A re-import is thousands of already-filed
    rows, and sending it for all of them would only inflate the response."""
    rows = csv_rows(("01/06/2026", "WOOLWORTHS METRO", "45.50"))
    send(auth_client, rows, account, commit=True)
    row = only_row(send(auth_client, rows, account, commit=False))
    assert row["status"] == "duplicate_exact"
    assert row["matched_neighbours"] == []
    assert row["matched_neighbours_total"] == 0
    # The match itself is still described — the reason stops being hover-only.
    assert row["matched_description"] == "WOOLWORTHS METRO"
    assert row["match_tier"] == "exact_hash"


# ------------------------------------------------------- nothing matched at all


def test_a_new_row_carries_every_match_field_null(
    auth_client: TestClient, account: str
) -> None:
    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "FIRST EVER", "10.00")),
             account, commit=False)
    )
    assert row["status"] == "new"
    for field in (
        "matched_txn_id", "matched_date", "matched_description", "matched_amount_cents",
        "matched_merchant", "matched_category_name", "matched_source",
        "matched_imported_at", "matched_imported_from", "matched_days_delta",
        "matched_wording", "match_tier",
    ):
        assert row[field] is None, field
    assert row["matched_neighbours"] == []


# ------------------------------------------------------------------- isolation


def test_a_batch_from_another_household_is_never_named(
    auth_client: TestClient, account: str
) -> None:
    """`load_candidates` carries no household predicate — it is safe only because
    the account was checked first. The batch lookup must re-scope rather than trust
    an id that arrived with a candidate, or a filename leaks across households.
    """
    from app import models
    from app.db import SessionLocal

    send(auth_client, csv_rows(("01/06/2026", "WOOLWORTHS METRO", "45.50")),
         account, commit=True)

    # Re-point the stored row at a batch belonging to nobody this household knows.
    with SessionLocal() as db:
        other = models.Household(name="Someone else", state="VIC")
        db.add(other)
        db.flush()
        foreign = models.ImportBatch(
            household_id=other.id, filename="their-private-statement.csv", file_format="csv"
        )
        db.add(foreign)
        db.flush()
        txn = db.query(models.Transaction).one()
        txn.import_batch_id = foreign.id
        db.commit()

    row = only_row(
        send(auth_client, csv_rows(("01/06/2026", "EFTPOS WOOLWORTHS 4521 NSW", "45.50")),
             account, commit=False)
    )
    assert row["matched_txn_id"] is not None, "the match itself should still be found"
    assert row["matched_imported_from"] is None
    assert row["matched_imported_at"] is None


# ----------------------------------------------------------------- the cost


def test_previewing_fifty_matches_costs_the_same_queries_as_one(
    auth_client: TestClient, account: str
) -> None:
    """The guard against an N+1 creeping into the row loop.

    Every match's provenance is resolved in one batched query up front, and the
    neighbourhood comes from candidates already in memory. Both are easy to turn
    into per-row queries by accident, and `dedup.py` spends three comment blocks
    keeping this path linear. Counting is the only way that stays true.
    """
    from app.db import engine

    history = [(f"{(i % 28) + 1:02d}/06/2026", f"MERCHANT {i}", f"{i + 1}.00") for i in range(50)]
    send(auth_client, csv_rows(*history), account, commit=True)

    def count_for(rows: list[tuple[str, str, str]]) -> int:
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            send(auth_client, csv_rows(*rows), account, commit=False)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    # Each of these re-presents stored rows with a changed reference number, so each
    # incoming row becomes a near match carrying provenance and a neighbourhood.
    one = count_for([(history[0][0], "MERCHANT 0 X9999", history[0][2])])
    fifty = count_for(
        [(d, f"{text} X9999", amount) for d, text, amount in history]
    )
    assert fifty == one, (
        f"previewing 50 matched rows issued {fifty} queries against {one} for a single "
        "row — the per-row work has grown a query"
    )


def test_the_neighbourhood_is_indexed_rather_than_scanned() -> None:
    """The quadratic guard, which this module has needed twice before.

    `neighbours()` is asked once per row that needs a decision, and the candidates it
    searches are the account's whole history. Reaching them by filtering that history
    — the obvious simplification — makes a 200-row review against a 20,000-row history
    four million comparisons. Two dict lookups per day of the window keeps it flat,
    and counting the rows touched is the only way that stays true.
    """
    from app.services.dedup import DuplicateMatcher
    from app.services.importers import ParsedTxn

    touched = 0
    # Every field read on a candidate, not just `id`. An earlier version of this test
    # counted `id` alone and passed happily against a full scan — because a scan
    # filters on amount, merchant and date and never looks at the id of a row it
    # rejects. The work a scan does is exactly the work that measure could not see.
    FIELDS = frozenset(
        {"id", "txn_date", "amount_cents", "raw_description", "merchant", "consumed"}
    )

    class Counted(dedup_module.Candidate):
        def __getattribute__(self, name: str) -> object:
            if name in FIELDS:
                nonlocal_touch()
            return object.__getattribute__(self, name)

    def nonlocal_touch() -> None:
        nonlocal touched
        touched += 1

    day = dt.date(2026, 1, 1)
    history = [
        Counted(
            id=f"c{i}",
            txn_date=day + dt.timedelta(days=i % 400),
            amount_cents=-4550,
            raw_description=f"WOOLWORTHS {i}",
            dedup_hash=f"h{i}",
            provider_txn_id=None,
            merchant="Woolworths",
        )
        for i in range(4000)
    ]
    matcher = DuplicateMatcher(history)

    touched = 0
    rows = 200
    for i in range(rows):
        matcher.neighbours(
            ParsedTxn(
                txn_date=day + dt.timedelta(days=i % 400),
                amount_cents=-4550,
                raw_description="EFTPOS WOOLWORTHS 4521",
                merchant="Woolworths",
            )
        )

    # The arithmetic, stated so a failure can be read rather than guessed at.
    #
    # 4,000 rows spread over 400 days is ~10 a day. Every one shares this amount AND
    # this merchant, so both indexes return all 10 — the worst case for this lookup.
    # A 15-day window is therefore ~300 candidate visits per row, and `id` is read
    # about three times per visit (the exclude check, the dict lookup, the insert).
    per_day = 4000 // 400
    window = 2 * dedup_module.NEIGHBOURHOOD_DAYS + 1
    visits = rows * per_day * 2 * window
    ceiling = visits * 8  # field reads per visit, generously
    # What a scan would cost, for contrast: every row walks the whole history, reading
    # at least an amount and a date off each to decide whether to keep it.
    scanning = rows * 4000 * 2
    assert ceiling < scanning, "the test would not distinguish indexed from scanned"
    assert touched <= ceiling, (
        f"neighbours() touched {touched:,} candidates for {rows} rows. Looking them "
        f"up in the window allows about {ceiling:,}; scanning the history would be at "
        f"least {scanning:,}. It is scanning rather than looking up."
    )
