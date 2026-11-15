"""What the 0.18.0 review found, pinned: which pair wins when evidence competes, short
wording, split transactions, a type corrected after linking, loan interest, a cash deposit
next to an import's edge, a person's own transfer category, and pairs an older image
links after a rollback."""

from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from test_transfer_kinds import _add, _cat, _row, _scan, _set, _summary, _wording

from app import models
from app.db import SessionLocal, engine
from app.dbinit import _classify_transfers_once
from app.services.categorise import PERSON_CONFIDENCE
from app.services.transfers import classify_transfers, learn_wording


def _household_id() -> str:
    with SessionLocal() as db:
        return db.execute(select(models.Household.id)).scalar_one()


def _suggestions(client: TestClient) -> list[tuple[str, str]]:
    return [
        (s["outflow"]["id"], s["inflow"]["id"])
        for s in client.get("/api/transfers/suggestions").json()
    ]


# --------------------------------------------------------------------------------------
# Which pair wins
# --------------------------------------------------------------------------------------


def test_the_cards_wording_beats_an_earlier_outflow_that_only_shares_the_amount(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _wording(auth_client, card, "By Authority To Cc")
    atm = _add(auth_client, everyday, "2026-06-03", -50000, "ATM WITHDRAWAL")
    payment = _add(auth_client, savings, "2026-06-05", -50000, "By Authority To Cc")
    arrived = _add(auth_client, card, "2026-06-05", 50000, "PAYMENT RECEIVED THANK YOU")

    assert _scan(auth_client)["linked"] == 2

    assert _row(payment).transfer_group_id == _row(arrived).transfer_group_id is not None
    assert _row(atm).transfer_group_id is None
    assert not _row(atm).is_transfer


def test_one_sides_wording_with_a_rival_is_for_a_person_and_offers_the_nearer(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _add(auth_client, everyday, "2026-06-03", -50000, "ATM WITHDRAWAL")
    payment = _add(auth_client, savings, "2026-06-05", -50000, "By Authority To Cc")
    arrived = _add(auth_client, card, "2026-06-05", 50000, "PAYMENT RECEIVED THANK YOU")

    scan = _scan(auth_client)

    # "Payment received" says the card was paid, not which of the two paid it.
    assert (scan["linked"], scan["to_review"]) == (0, 1)
    assert _suggestions(auth_client) == [(payment, arrived)]


def test_the_stronger_evidence_wins_over_the_nearer_pair(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _wording(auth_client, card, "By Authority To Cc")
    # A transfer word on both sides, the same day; the card's own wording, two days on.
    nearer = _add(auth_client, everyday, "2026-06-03", -50000, "TRANSFER TO CARD")
    named = _add(auth_client, savings, "2026-06-05", -50000, "By Authority To Cc")
    arrived = _add(auth_client, card, "2026-06-03", 50000, "TRANSFER RECEIVED")

    _scan(auth_client)

    assert _row(named).transfer_group_id == _row(arrived).transfer_group_id is not None
    assert _row(nearer).transfer_group_id is None


def test_one_sides_wording_with_no_rival_links(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _add(auth_client, savings, "2026-06-05", -50000, "By Authority To Cc")
    _add(auth_client, card, "2026-06-06", 50000, "PAYMENT RECEIVED THANK YOU")

    assert _scan(auth_client)["linked"] == 2


def test_wording_for_one_card_beats_a_transfer_word_into_another_a_day_nearer(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    amex = create_account(auth_client, "Amex", "credit_card")["id"]
    visa = create_account(auth_client, "Visa", "credit_card")["id"]
    _wording(auth_client, amex, "By Authority To Amex")
    out = _add(auth_client, savings, "2026-06-05", -50000, "By Authority To Amex")
    to_amex = _add(auth_client, amex, "2026-06-07", 50000, "PAYMENT RECEIVED")
    to_visa = _add(auth_client, visa, "2026-06-06", 50000, "TRANSFER RECEIVED")

    _scan(auth_client)

    assert _row(out).transfer_group_id == _row(to_amex).transfer_group_id is not None
    assert _row(to_visa).transfer_group_id is None


# --------------------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------------------


def test_wording_too_short_to_tell_anything_apart_is_refused(auth_client: TestClient) -> None:
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    for phrase in ("Cc", "x1", "A.B.C"):
        resp = auth_client.patch(f"/api/accounts/{card}", json={"transfer_wording": [phrase]})
        assert resp.status_code == 422, phrase
        assert "too short" in resp.json()["detail"]
    assert _wording(auth_client, card, "Visa")["transfer_wording"] == ["Visa"]


def test_wording_matches_whole_words_only(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _wording(auth_client, card, "Card Pay")
    _add(auth_client, everyday, "2026-06-05", -18900, "CARD PAYMENTS AUSTRALIA")
    _add(auth_client, card, "2026-06-06", 18900, "REFUND JB HIFI")

    assert _scan(auth_client)["linked"] == 0


# --------------------------------------------------------------------------------------
# Cash deposits and review
# --------------------------------------------------------------------------------------


def test_an_import_leaves_a_deposit_alone_when_its_other_half_is_before_the_import(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, everyday, "2026-06-01", -50000, "PAYMENT")
    deposit = _add(auth_client, savings, "2026-06-03", 50000, "DEPOSIT")

    with SessionLocal() as db:
        # An import whose earliest row is the 5th: the 1st is out of its sight.
        classify_transfers(db, _household_id(), since=dt.date(2026, 6, 5))

    assert _row(deposit).category_id is None
    assert len(_suggestions(auth_client)) == 1


def test_a_cash_deposit_goes_back_to_blank_while_its_pair_waits_for_review(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    deposit = _add(auth_client, savings, "2026-06-03", 50000, "DEPOSIT")
    assert _scan(auth_client)["cash_deposits"] == 1
    assert _row(deposit).category_id == _cat(auth_client, "Cash deposit")

    _add(auth_client, everyday, "2026-06-03", -50000, "PAYMENT")
    scan = _scan(auth_client)

    assert (scan["to_review"], scan["cash_deposits"]) == (1, 0)
    assert (_row(deposit).category_id, _row(deposit).confidence) == (None, None)


def test_a_locked_cash_deposit_stays_while_its_pair_waits(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    deposit = _add(auth_client, savings, "2026-06-03", 50000, "DEPOSIT")
    _scan(auth_client)
    _set(deposit, category_locked=True)
    _add(auth_client, everyday, "2026-06-03", -50000, "PAYMENT")

    assert _scan(auth_client)["to_review"] == 1

    assert _row(deposit).category_id == _cat(auth_client, "Cash deposit")


def test_a_persons_cash_deposit_stays_while_its_pair_waits(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    deposit = _add(auth_client, savings, "2026-06-03", 50000, "DEPOSIT")
    _set(deposit, category_id=_cat(auth_client, "Cash deposit"), confidence=PERSON_CONFIDENCE)
    _add(auth_client, everyday, "2026-06-03", -50000, "PAYMENT")

    _scan(auth_client)

    assert _row(deposit).category_id == _cat(auth_client, "Cash deposit")


def test_a_persons_own_internal_transfers_is_kept_when_linking(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    internal = _cat(auth_client, "Internal transfers")
    out = _add(auth_client, savings, "2026-06-05", -125492, "TRANSFER TO LOAN")
    _set(out, category_id=internal, confidence=PERSON_CONFIDENCE)
    _add(auth_client, loan, "2026-06-05", 125492, "TRANSFER FROM SAVINGS")

    assert _scan(auth_client)["linked"] == 2

    row = _row(out)
    assert row.transfer_kind == "loan_repayment"
    assert (row.category_id, row.confidence) == (internal, PERSON_CONFIDENCE)


def test_an_older_releases_unscored_internal_transfers_is_still_refiled(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    out = _add(auth_client, savings, "2026-06-05", -5000, "TRANSFER TO VISA")
    _set(out, category_id=_cat(auth_client, "Internal transfers"), confidence=None)
    _add(auth_client, card, "2026-06-05", 5000, "TRANSFER FROM SAVINGS")

    _scan(auth_client)

    assert _row(out).category_id == _cat(auth_client, "Credit-card payments")


# --------------------------------------------------------------------------------------
# Splits
# --------------------------------------------------------------------------------------


def _split(client: TestClient, txn_id: str, *amounts: int) -> list[str]:
    resp = client.post(
        f"/api/transactions/{txn_id}/split",
        json={"splits": [{"amount_cents": a} for a in amounts]},
    )
    assert resp.status_code == 200, resp.text
    return [c["id"] for c in resp.json()]


def test_a_split_transaction_is_not_paired_offered_or_linked(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    whole = _add(auth_client, everyday, "2026-06-05", -50000, "PAYMENT TO VISA")
    _split(auth_client, whole, -30000, -20000)
    arrived = _add(auth_client, card, "2026-06-05", 50000, "PAYMENT RECEIVED THANK YOU")

    assert _scan(auth_client)["linked"] == 0
    assert _suggestions(auth_client) == []
    offered = [c["id"] for c in auth_client.get(f"/api/transfers/candidates/{arrived}").json()]
    assert whole not in offered
    resp = auth_client.post("/api/transfers/link", json={"first_id": whole, "second_id": arrived})
    assert resp.status_code == 400
    assert "split" in resp.json()["detail"]


def test_a_leg_split_after_linking_keeps_its_link_whole(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    out = _add(auth_client, everyday, "2026-06-05", -100000, "TRANSFER TO SAVINGS")
    _add(auth_client, savings, "2026-06-05", 100000, "TRANSFER FROM EVERYDAY")
    assert _scan(auth_client)["linked"] == 2
    group = _row(out).transfer_group_id
    parts = _split(auth_client, out, -60000, -40000)
    assert all(_row(p).transfer_kind == "own_accounts" for p in parts)

    listed = auth_client.get("/api/transfers/recent").json()
    assert [g["group_id"] for g in listed] == [group]
    assert listed[0]["outflow"]["id"] == out

    loan_kind = auth_client.post(f"/api/transfers/{group}/kind", json={"kind": "loan_repayment"})
    assert loan_kind.status_code == 200, loan_kind.text
    # The parts are what the totals count, so they are counted the way the kind says.
    assert all(
        (_row(p).transfer_kind, _row(p).is_transfer) == ("loan_repayment", False) for p in parts
    )
    assert _summary(auth_client)["expense_cents"] == 100000

    assert auth_client.post(f"/api/transfers/{group}/unlink").status_code == 200
    with SessionLocal() as db:
        assert db.execute(select(models.TransferDismissal)).scalars().all()
    _scan(auth_client)
    assert _row(out).transfer_group_id is None


# --------------------------------------------------------------------------------------
# An account's type, put right after linking
# --------------------------------------------------------------------------------------


def test_correcting_an_accounts_type_recounts_what_was_linked_into_it(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac", "everyday")["id"]  # imported as everyday
    out = _add(auth_client, savings, "2026-06-23", -125492, "TRANSFER TO LOAN")
    _add(auth_client, loan, "2026-06-23", 125492, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    assert _row(out).transfer_kind == "own_accounts"
    assert _summary(auth_client)["expense_cents"] == 0

    resp = auth_client.patch(f"/api/accounts/{loan}", json={"type": "home_loan"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["transfers_rekinded"] == 1
    row = _row(out)
    assert (row.transfer_kind, row.is_transfer) == ("loan_repayment", False)
    assert row.category_id == _cat(auth_client, "Mortgage")
    assert _summary(auth_client)["expense_cents"] == 125492


def test_a_kind_a_person_chose_survives_a_type_change(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    other = create_account(auth_client, "Other", "everyday")["id"]
    out = _add(auth_client, savings, "2026-06-23", -5000, "TRANSFER OUT")
    _add(auth_client, other, "2026-06-23", 5000, "TRANSFER IN")
    _scan(auth_client)
    group = _row(out).transfer_group_id
    auth_client.post(f"/api/transfers/{group}/kind", json={"kind": "card_payment"})

    resp = auth_client.patch(f"/api/accounts/{other}", json={"type": "home_loan"})

    assert resp.json()["transfers_rekinded"] == 0
    assert _row(out).transfer_kind == "card_payment"


# --------------------------------------------------------------------------------------
# Loan interest
# --------------------------------------------------------------------------------------


def test_a_loans_own_interest_is_not_counted_on_top_of_the_repayment(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    _add(auth_client, savings, "2026-06-23", -250000, "By Authority To Westpac")
    _add(auth_client, loan, "2026-06-23", 250000, "REPAYMENT")
    _add(auth_client, loan, "2026-06-30", -180000, "INTEREST CHARGED")
    _scan(auth_client)

    summary = _summary(auth_client)

    # $2,500 left the household; the $1,800 interest is inside it, not on top of it.
    assert (summary["expense_cents"], summary["income_cents"]) == (250000, 0)


# --------------------------------------------------------------------------------------
# After a rollback
# --------------------------------------------------------------------------------------


def test_pairs_an_older_image_linked_after_a_rollback_get_their_kind(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    out = _add(auth_client, savings, "2026-06-23", -125492, "P")
    inflow = _add(auth_client, loan, "2026-06-23", 125492, "Q")
    internal = _cat(auth_client, "Internal transfers")
    for txn in (out, inflow):  # as 0.17 links a pair: no kind, both left out
        _set(txn, is_transfer=True, transfer_group_id="old", category_id=internal, confidence=None)
    with SessionLocal() as db:
        assert db.execute(select(models.Household.transfers_classified_at)).scalar_one()

    _classify_transfers_once(engine)

    row = _row(out)
    assert (row.transfer_kind, row.is_transfer) == ("loan_repayment", False)


# --------------------------------------------------------------------------------------
# Accepting, and the linked list
# --------------------------------------------------------------------------------------


def test_accepting_with_learning_links_the_others_with_that_wording_at_once(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    first = _add(auth_client, savings, "2026-06-01", -10000, "By Authority To Cc 0412")
    first_in = _add(auth_client, card, "2026-06-01", 10000, "CREDIT")
    second = _add(auth_client, savings, "2026-06-15", -20000, "By Authority To Cc 0412")
    _add(auth_client, card, "2026-06-15", 20000, "CREDIT")
    assert _scan(auth_client)["to_review"] == 2

    resp = auth_client.post(
        "/api/transfers/accept", json={"outflow_id": first, "inflow_id": first_in}
    )

    assert resp.status_code == 200, resp.text
    assert (resp.json()["learned"], resp.json()["also_linked"]) == ("by authority to cc", 1)
    assert _row(second).transfer_kind == "card_payment"
    assert _suggestions(auth_client) == []


def test_the_linked_list_takes_a_few_queries_however_long_it_is(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]

    def queries() -> int:
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            assert auth_client.get("/api/transfers/recent").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    def add_pairs(start: int, n: int) -> None:
        for i in range(start, start + n):
            day = f"2026-06-{(i % 28) + 1:02d}"
            _add(auth_client, everyday, day, -(1000 + i), "TRANSFER TO SAVINGS")
            _add(auth_client, savings, day, 1000 + i, "TRANSFER FROM EVERYDAY")
        _scan(auth_client)

    add_pairs(0, 2)
    two = queries()
    add_pairs(2, 20)
    assert len(auth_client.get("/api/transfers/recent").json()) == 22
    assert queries() == two


def test_wording_already_known_is_not_learned_twice_and_whole_words_decide() -> None:
    account = models.Account(
        name="Visa", type="credit_card", transfer_wording=["By Authority To Cc"]
    )
    assert learn_wording(account, "BY AUTHORITY TO CC 0412") is None
    assert account.transfer_wording == ["By Authority To Cc"]
    # "to cc" is not a whole-word part of "to ccx": something new to learn.
    other = models.Account(name="Amex", type="credit_card", transfer_wording=["To Cc"])
    assert learn_wording(other, "By Authority To Ccx") == "by authority to ccx"


def test_a_loans_own_interest_raises_no_large_payment_alert(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    everyday = create_account(auth_client, "Everyday")["id"]
    today = dt.date.today().isoformat()
    _add(auth_client, loan, today, -180000, "INTEREST CHARGED")
    _add(auth_client, everyday, today, -60000, "BIG TV PURCHASE")

    alerts = auth_client.get("/api/notifications").json()["items"]

    large = [a["body"] for a in alerts if a["type"] == "large_txn"]
    assert len(large) == 1
    assert "BIG TV" in large[0].upper()

