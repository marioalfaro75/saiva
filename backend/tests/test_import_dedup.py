"""Duplicate detection on import: the three matching tiers, and the guarantee that
genuine repeated transactions are not mistaken for duplicates (PRD R6)."""

from __future__ import annotations

import json

from conftest import create_account
from fastapi.testclient import TestClient

from app.services import dedup

HEADER = "Date,Description,Amount\n"
MAPPING = json.dumps(
    {"has_header": True, "date_col": 0, "description_col": 1, "amount_mode": "single",
     "amount_col": 2}
)


def _csv(*lines: str) -> bytes:
    return (HEADER + "\n".join(lines) + "\n").encode()


def _commit(client: TestClient, account_id: str, content: bytes, **extra: str) -> dict:
    resp = client.post(
        "/api/imports/commit",
        files={"file": ("stmt.csv", content, "text/csv")},
        data={"account_id": account_id, "file_format": "csv", "mapping": MAPPING, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _preview(client: TestClient, account_id: str, content: bytes) -> dict:
    resp = client.post(
        "/api/imports/preview",
        files={"file": ("stmt.csv", content, "text/csv")},
        data={"account_id": account_id, "file_format": "csv", "mapping": MAPPING},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_genuine_same_day_repeats_are_both_imported(auth_client: TestClient) -> None:
    """Two identical real purchases on one day are two transactions, not a duplicate."""
    account = create_account(auth_client)
    twice = _csv(
        "01/06/2025,CAMPOS COFFEE NEWTOWN,-4.50",
        "01/06/2025,CAMPOS COFFEE NEWTOWN,-4.50",
    )
    assert _commit(auth_client, account["id"], twice)["added"] == 2

    txns = auth_client.get("/api/transactions", params={"q": "campos"}).json()
    assert txns["total"] == 2

    # …but re-importing that same file adds nothing: two stored rows now match the two
    # incoming ones.
    again = _commit(auth_client, account["id"], twice)
    assert (again["added"], again["skipped"]) == (0, 2)


def test_partial_repeat_imports_only_the_extra_occurrence(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _commit(auth_client, account["id"], _csv("01/06/2025,ATM WITHDRAWAL,-20.00"))
    # The next statement shows the same day had two identical withdrawals.
    result = _commit(
        auth_client,
        account["id"],
        _csv("01/06/2025,ATM WITHDRAWAL,-20.00", "01/06/2025,ATM WITHDRAWAL,-20.00"),
    )
    assert (result["added"], result["skipped"]) == (1, 1)
    assert auth_client.get("/api/transactions", params={"q": "atm"}).json()["total"] == 2


def test_overlapping_date_ranges_skip_only_the_overlap(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    first = _csv(
        "01/06/2025,WOOLWORTHS METRO,-30.00",
        "02/06/2025,BP CONNECT,-70.00",
        "03/06/2025,NETFLIX.COM,-19.99",
    )
    assert _commit(auth_client, account["id"], first)["added"] == 3

    overlapping = _csv(
        "02/06/2025,BP CONNECT,-70.00",       # already imported
        "03/06/2025,NETFLIX.COM,-19.99",      # already imported
        "04/06/2025,COLES EXPRESS,-45.00",    # new
    )
    result = _commit(auth_client, account["id"], overlapping)
    assert (result["added"], result["skipped"]) == (1, 2)


def test_redated_transaction_is_flagged_probable_and_skipped(auth_client: TestClient) -> None:
    """Pending -> posted re-dating: same amount and wording, two days later."""
    account = create_account(auth_client)
    _commit(auth_client, account["id"], _csv("01/06/2025,QANTAS AIRWAYS,-345.60"))

    redated = _csv("03/06/2025,QANTAS AIRWAYS,-345.60")
    preview = _preview(auth_client, account["id"], redated)
    row = preview["rows"][0]
    assert row["status"] == "duplicate_probable"
    assert row["will_import"] is False
    assert "2 days apart" in row["duplicate_reason"]
    assert row["matched_date"] == "2025-06-01"
    assert preview["probable_count"] == 1

    assert _commit(auth_client, account["id"], redated)["added"] == 0


def test_changed_reference_number_is_flagged_probable(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _commit(auth_client, account["id"], _csv("01/06/2025,WOOLWORTHS 1234 SYDNEY,-85.40"))
    # Same purchase, but the export now carries a different store/reference number.
    preview = _preview(auth_client, account["id"], _csv("01/06/2025,WOOLWORTHS 9876 SYDNEY,-85.40"))
    assert preview["rows"][0]["status"] == "duplicate_probable"


def test_probable_duplicate_can_be_forced_in(auth_client: TestClient) -> None:
    """A genuine second identical purchase a day later is the reviewer's call."""
    account = create_account(auth_client)
    _commit(auth_client, account["id"], _csv("01/06/2025,CAMPOS COFFEE,-4.50"))

    second = _csv("02/06/2025,CAMPOS COFFEE,-4.50")
    assert _preview(auth_client, account["id"], second)["rows"][0]["status"] == "duplicate_probable"

    forced = _commit(auth_client, account["id"], second, force_import=json.dumps([0]))
    assert forced["added"] == 1


def test_definite_duplicates_cannot_be_forced_in(auth_client: TestClient) -> None:
    """Overriding an exact match would knowingly create a double-up, so it is refused."""
    account = create_account(auth_client)
    row = _csv("01/06/2025,NETFLIX.COM,-19.99")
    _commit(auth_client, account["id"], row)
    again = _commit(auth_client, account["id"], row, force_import=json.dumps([0]))
    assert (again["added"], again["skipped"]) == (0, 1)


def test_new_row_can_be_skipped_by_the_reviewer(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    two = _csv("01/06/2025,WOOLWORTHS,-30.00", "02/06/2025,BP CONNECT,-70.00")
    result = _commit(auth_client, account["id"], two, force_skip=json.dumps([1]))
    assert (result["added"], result["skipped"]) == (1, 1)


def test_preview_reports_every_row_with_a_verdict(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _commit(auth_client, account["id"], _csv("01/06/2025,WOOLWORTHS,-30.00"))
    preview = _preview(
        auth_client,
        account["id"],
        _csv("01/06/2025,WOOLWORTHS,-30.00", "05/06/2025,COLES,-12.00"),
    )
    assert preview["total_rows"] == 2
    assert [r["status"] for r in preview["rows"]] == ["duplicate_exact", "new"]
    assert [r["row_index"] for r in preview["rows"]] == [0, 1]
    assert (preview["new_count"], preview["duplicate_count"]) == (1, 1)


def test_commit_rejects_malformed_decisions(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    resp = auth_client.post(
        "/api/imports/commit",
        files={"file": ("stmt.csv", _csv("01/06/2025,COLES,-12.00"), "text/csv")},
        data={
            "account_id": account["id"], "file_format": "csv", "mapping": MAPPING,
            "force_import": '{"not": "a list"}',
        },
    )
    assert resp.status_code == 400


# ------------------------------------------------------------------ provider id (FITID)

OFX_TEMPLATE = """<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{date}<TRNAMT>-42.00<FITID>{fitid}<NAME>{name}</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""


def _ofx(client: TestClient, account_id: str, date: str, fitid: str, name: str) -> dict:
    body = OFX_TEMPLATE.format(date=date, fitid=fitid, name=name).encode()
    resp = client.post(
        "/api/imports/commit",
        files={"file": ("s.ofx", body, "application/x-ofx")},
        data={"account_id": account_id, "file_format": "ofx"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_provider_id_matches_through_date_and_description_drift(auth_client: TestClient) -> None:
    """FITID is the bank's own id: it identifies the transaction even when the date
    and the wording both change between exports."""
    account = create_account(auth_client, "Savings", "savings")
    assert _ofx(auth_client, account["id"], "20250601", "TXN-99", "PENDING PURCHASE")["added"] == 1

    drifted = _ofx(auth_client, account["id"], "20250605", "TXN-77", "HARVEY NORMAN AUBURN")
    assert drifted["added"] == 1  # different id and wording -> genuinely new

    same = _ofx(auth_client, account["id"], "20250604", "TXN-99", "HARVEY NORMAN AUBURN")
    assert (same["added"], same["skipped"]) == (0, 1)


def test_ofx_import_stores_the_provider_id(auth_client: TestClient) -> None:
    from app import models
    from app.db import SessionLocal

    account = create_account(auth_client, "Savings", "savings")
    _ofx(auth_client, account["id"], "20250601", "FIT-1", "COLES")
    with SessionLocal() as db:
        txn = db.query(models.Transaction).filter_by(account_id=account["id"]).one()
        assert txn.provider_txn_id == "FIT-1"


# ------------------------------------------------------------------------ unit level


def test_fuzzy_norm_strips_reference_numbers() -> None:
    assert dedup.fuzzy_norm("WOOLWORTHS 1234 SYDNEY") == dedup.fuzzy_norm("WOOLWORTHS 9876 SYDNEY")
    assert dedup.fuzzy_norm("VISA XXXX4417 UBER") == dedup.fuzzy_norm("VISA XXXX9999 UBER")


def test_similarity_separates_different_merchants() -> None:
    a, b = dedup.fuzzy_norm("QANTAS AIRWAYS"), dedup.fuzzy_norm("KMART BONDI JUNCTION")
    assert dedup.similarity(a, b) < dedup.SIMILARITY_THRESHOLD


def test_different_merchants_same_amount_and_day_are_not_duplicates(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)
    _commit(auth_client, account["id"], _csv("01/06/2025,QANTAS AIRWAYS,-42.00"))
    result = _commit(auth_client, account["id"], _csv("01/06/2025,KMART BONDI JUNCTION,-42.00"))
    assert result["added"] == 1
