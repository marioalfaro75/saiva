"""Transfers by kind: card payments, money between accounts, loan repayments, cash deposits.

Asked for: a payment from savings to the credit card is a card payment, not another
expense (the card's purchases are the spending); a deposit into savings from none of the
household's accounts is a cash deposit, counted as income; savings to the home loan is a
repayment, counted as Mortgage spending. Matching uses the bank's wording, the
household's own wording per account (configurable, and learned from accepted
suggestions), and the account's number; a pair with nothing but the amount and dates in
common waits for review. The household's examples: "By Authority To Cc" (savings to
card) and "By Authority To Westpac" (savings to the home loan).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from conftest import create_account
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.db import SessionLocal, engine
from app.dbinit import _classify_transfers_once
from app.services import notifications
from app.services.categorise import ML_MAX_CONFIDENCE
from app.services.notes import suggest_note

JUNE = {"period": "custom", "start": "2026-06-01", "end": "2026-06-30"}


def _add(client: TestClient, account_id: str, date: str, cents: int, desc: str) -> str:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": date,
            "amount_cents": cents,
            "description": desc,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _cat(client: TestClient, name: str) -> str:
    return next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name)


def _row(txn_id: str) -> models.Transaction:
    with SessionLocal() as db:
        t = db.get(models.Transaction, txn_id)
        assert t is not None
        db.expunge(t)
        return t


def _set(txn_id: str, **fields: object) -> None:
    with SessionLocal() as db:
        t = db.get(models.Transaction, txn_id)
        assert t is not None
        for key, value in fields.items():
            setattr(t, key, value)
        db.commit()


def _wording(client: TestClient, account_id: str, *phrases: str) -> dict:
    resp = client.patch(f"/api/accounts/{account_id}", json={"transfer_wording": list(phrases)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _scan(client: TestClient) -> dict:
    resp = client.post("/api/transactions/detect-transfers")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _summary(client: TestClient) -> dict:
    return client.get("/api/dashboard/summary", params=JUNE).json()


# --------------------------------------------------------------------------------------
# The three kinds, as the household described them
# --------------------------------------------------------------------------------------


def test_savings_to_the_credit_card_is_a_card_payment_not_more_spending(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _wording(auth_client, card, "By Authority To Cc")
    groceries = _add(auth_client, card, "2026-06-03", -821370, "WOOLWORTHS 1234 SYDNEY")
    out = _add(auth_client, savings, "2026-06-29", -821370, "By Authority To Cc")
    inflow = _add(auth_client, card, "2026-06-30", 821370, "Direct debit received")

    assert _scan(auth_client)["linked"] == 2

    card_payments = _cat(auth_client, "Credit-card payments")
    for leg in (out, inflow):
        row = _row(leg)
        assert row.transfer_kind == "card_payment"
        assert row.is_transfer
        assert (row.category_id, row.confidence) == (card_payments, ML_MAX_CONFIDENCE)
    assert _row(out).transfer_group_id == _row(inflow).transfer_group_id
    # The card's purchase is the spending; the payment is neither spending nor income.
    summary = _summary(auth_client)
    assert summary["expense_cents"] == 821370
    assert summary["income_cents"] == 0
    assert _row(groceries).transfer_group_id is None


def test_savings_to_the_home_loan_is_a_repayment_counted_as_mortgage_spending(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    out = _add(auth_client, savings, "2026-06-23", -125492, "By Authority To Westpac")
    inflow = _add(auth_client, loan, "2026-06-23", 125492, "LOAN REPAYMENT")

    _scan(auth_client)

    mortgage = _cat(auth_client, "Mortgage")
    paying, receiving = _row(out), _row(inflow)
    assert paying.transfer_kind == receiving.transfer_kind == "loan_repayment"
    assert (paying.is_transfer, paying.category_id) == (False, mortgage)
    assert receiving.is_transfer
    summary = _summary(auth_client)
    assert summary["expense_cents"] == 125492  # the repayment is spending
    assert summary["income_cents"] == 0  # the loan's side is not income
    budget_names = [
        i["category_name"]
        for i in auth_client.get("/api/dashboard/categories", params=JUNE).json()["items"]
    ]
    assert "Mortgage" in budget_names


def test_everyday_to_savings_is_money_between_accounts_counted_nowhere(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    out = _add(auth_client, everyday, "2026-06-10", -50000, "TRANSFER TO SAVINGS")
    inflow = _add(auth_client, savings, "2026-06-11", 50000, "TRANSFER FROM EVERYDAY")

    _scan(auth_client)

    internal = _cat(auth_client, "Internal transfers")
    for leg in (out, inflow):
        assert (_row(leg).transfer_kind, _row(leg).category_id) == ("own_accounts", internal)
    assert _summary(auth_client)["expense_cents"] == 0


def test_a_deposit_into_savings_from_no_other_account_is_a_cash_deposit_counted_as_income(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    gift = _add(auth_client, savings, "2026-06-12", 100000, "DEPOSIT")
    into_everyday = _add(auth_client, everyday, "2026-06-12", 7000, "DEPOSIT")

    assert _scan(auth_client)["cash_deposits"] == 1

    assert _row(gift).category_id == _cat(auth_client, "Cash deposit")
    assert _row(into_everyday).category_id is None  # only savings and offset
    assert _summary(auth_client)["income_cents"] == 107000


def test_a_cash_deposit_is_refiled_when_its_other_half_turns_up(auth_client: TestClient) -> None:
    # The savings file was imported first, so the deposit looked like it came from
    # nowhere; then the everyday file arrived with the transfer that made it.
    savings = create_account(auth_client, "Savings", "savings")["id"]
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    deposit = _add(auth_client, savings, "2026-06-12", 25000, "TRANSFER FROM EVERYDAY")
    _scan(auth_client)
    assert _row(deposit).category_id == _cat(auth_client, "Cash deposit")

    _add(auth_client, everyday, "2026-06-12", -25000, "TRANSFER TO SAVINGS")
    _scan(auth_client)

    assert _row(deposit).transfer_kind == "own_accounts"
    assert _row(deposit).category_id == _cat(auth_client, "Internal transfers")


def test_a_cash_deposit_never_replaces_a_persons_uncategorised(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    deposit = _add(auth_client, savings, "2026-06-12", 25000, "DEPOSIT")
    auth_client.post(
        f"/api/transactions/{deposit}/recategorise", json={"category_id": None, "scope": "none"}
    )
    _scan(auth_client)
    assert (_row(deposit).category_id, _row(deposit).confidence) == (None, 1.0)


# --------------------------------------------------------------------------------------
# Evidence, and review
# --------------------------------------------------------------------------------------


def test_a_pair_with_only_the_amount_in_common_waits_for_review(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    out = _add(auth_client, savings, "2026-06-29", -821370, "By Authority To Cc")
    inflow = _add(auth_client, card, "2026-07-01", 821370, "DIRECT DEBIT 0099")

    scan = _scan(auth_client)

    assert (scan["linked"], scan["to_review"]) == (0, 1)
    assert _row(out).transfer_group_id is None
    suggestions = auth_client.get("/api/transfers/suggestions").json()
    assert [(s["outflow"]["id"], s["inflow"]["id"]) for s in suggestions] == [(out, inflow)]
    assert suggestions[0]["kind"] == "card_payment"
    assert suggestions[0]["days_apart"] == 2


def test_accepting_a_suggestion_learns_the_wording_so_the_next_one_links_itself(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    out = _add(auth_client, savings, "2026-05-29", -50000, "By Authority To Cc 0412")
    inflow = _add(auth_client, card, "2026-05-30", 50000, "DIRECT DEBIT")

    resp = auth_client.post("/api/transfers/accept", json={"outflow_id": out, "inflow_id": inflow})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["learned"], body["learned_for"]) == ("by authority to cc", "Visa")
    assert body["group"]["kind"] == "card_payment"
    assert auth_client.get(f"/api/accounts/{card}").json()["transfer_wording"] == [
        "by authority to cc"
    ]
    # Next month's payment links by itself.
    out2 = _add(auth_client, savings, "2026-06-29", -61000, "By Authority To Cc 0412")
    _add(auth_client, card, "2026-06-30", 61000, "DIRECT DEBIT")
    assert _scan(auth_client)["linked"] == 2
    assert _row(out2).transfer_kind == "card_payment"


def test_a_dismissed_pair_is_never_suggested_or_linked_again(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    out = _add(auth_client, everyday, "2026-06-01", -200000, "RENT JUNE")
    inflow = _add(auth_client, savings, "2026-06-02", 200000, "INTEREST BONUS")
    assert len(auth_client.get("/api/transfers/suggestions").json()) == 1

    assert (
        auth_client.post(
            "/api/transfers/dismiss", json={"outflow_id": out, "inflow_id": inflow}
        ).status_code
        == 204
    )

    assert auth_client.get("/api/transfers/suggestions").json() == []
    _wording(auth_client, savings, "rent june")  # even evidence added later
    _scan(auth_client)
    assert _row(out).transfer_group_id is None


def test_an_account_number_in_the_description_links_a_pair(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    with SessionLocal() as db:
        account = db.get(models.Account, card)
        assert account is not None
        account.bank_identifier = "4564 7890 1234 5678"
        db.commit()
    out = _add(auth_client, savings, "2026-06-20", -30000, "BPAY 45647890 12345678")
    _add(auth_client, card, "2026-06-20", 30000, "CREDIT 0001")

    assert _scan(auth_client)["linked"] == 2
    assert _row(out).transfer_kind == "card_payment"


def test_a_salary_a_person_filed_is_not_offered_as_a_transfer(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, everyday, "2026-06-01", -380000, "RENT")
    salary = _add(auth_client, savings, "2026-06-01", 380000, "ACME PTY LTD")
    auth_client.post(
        f"/api/transactions/{salary}/recategorise",
        json={"category_id": _cat(auth_client, "Salary/Wages"), "scope": "none"},
    )
    assert auth_client.get("/api/transfers/suggestions").json() == []


# --------------------------------------------------------------------------------------
# A person's corrections
# --------------------------------------------------------------------------------------


def test_unlinking_releases_both_sides_and_is_remembered(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    out = _add(auth_client, everyday, "2026-06-10", -50000, "TRANSFER TO SAVINGS")
    inflow = _add(auth_client, savings, "2026-06-10", 50000, "TRANSFER FROM EVERYDAY")
    _scan(auth_client)
    group = _row(out).transfer_group_id

    resp = auth_client.post(f"/api/transfers/{group}/unlink")

    assert resp.json() == {"updated": 2}
    for leg in (out, inflow):
        row = _row(leg)
        assert (row.transfer_group_id, row.transfer_kind, row.is_transfer) == (None, None, False)
        assert row.category_id is None  # Internal transfers was the link's, not a choice
    _scan(auth_client)
    assert _row(out).transfer_group_id is None


def test_undoing_a_loan_repayment_leaves_the_payment_as_mortgage_and_not_the_loan_side_as_income(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    out = _add(auth_client, savings, "2026-06-23", -125492, "TRANSFER TO LOAN")
    inflow = _add(auth_client, loan, "2026-06-23", 125492, "TRANSFER FROM SAVINGS")
    _scan(auth_client)

    auth_client.post(f"/api/transfers/{_row(out).transfer_group_id}/unlink")

    assert _row(out).category_id == _cat(auth_client, "Mortgage")
    assert _row(inflow).category_id is None


def test_changing_the_kind_refiles_both_sides(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    out = _add(auth_client, savings, "2026-06-29", -10000, "TRANSFER TO VISA")
    _add(auth_client, card, "2026-06-29", 10000, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    group = _row(out).transfer_group_id

    resp = auth_client.post(f"/api/transfers/{group}/kind", json={"kind": "own_accounts"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "own_accounts"
    assert _row(out).category_id == _cat(auth_client, "Internal transfers")


def test_linking_by_hand_and_the_candidates_offered(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    out = _add(auth_client, savings, "2026-06-01", -10000, "PAYMENT")
    near = _add(auth_client, card, "2026-06-05", 10000, "CREDIT")
    _add(auth_client, card, "2026-06-02", 9000, "CREDIT")
    _add(auth_client, card, "2026-06-30", 10000, "CREDIT")  # outside two weeks
    _add(auth_client, savings, "2026-06-02", 10000, "SAME ACCOUNT")

    offered = [t["id"] for t in auth_client.get(f"/api/transfers/candidates/{out}").json()]
    assert offered[0] == near and len(offered) == 2

    resp = auth_client.post("/api/transfers/link", json={"first_id": near, "second_id": out})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["outflow"]["id"], resp.json()["inflow"]["id"]) == (out, near)
    assert resp.json()["outflow"]["counterpart_account_name"] == "Visa"
    again = auth_client.post("/api/transfers/link", json={"first_id": near, "second_id": out})
    assert again.status_code == 409


def test_a_link_needs_two_accounts_and_two_directions(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    a = _add(auth_client, savings, "2026-06-01", -10000, "A")
    b = _add(auth_client, savings, "2026-06-01", 10000, "B")
    assert (
        auth_client.post("/api/transfers/link", json={"first_id": a, "second_id": b}).status_code
        == 400
    )


def test_the_list_shows_each_sides_kind_and_other_account(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, everyday, "2026-06-10", -50000, "TRANSFER TO SAVINGS")
    _add(auth_client, savings, "2026-06-10", 50000, "TRANSFER FROM EVERYDAY")
    _scan(auth_client)

    items = auth_client.get("/api/transactions", params={"q": "transfer"}).json()["items"]
    sides = {i["account_name"]: i["counterpart_account_name"] for i in items}
    assert sides == {"Everyday": "Savings", "Savings": "Everyday"}
    assert {i["transfer_kind"] for i in items} == {"own_accounts"}
    recent = auth_client.get("/api/transfers/recent").json()
    assert len(recent) == 1 and recent[0]["kind"] == "own_accounts"


def test_rules_leave_a_linked_repayment_to_its_link(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    out = _add(auth_client, savings, "2026-06-23", -125492, "TRANSFER TO LOAN")
    _add(auth_client, loan, "2026-06-23", 125492, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    takeaway = _cat(auth_client, "Takeaway")

    made = auth_client.post(
        "/api/rules",
        json={"match_type": "contains", "pattern": "transfer to loan", "category_id": takeaway},
    ).json()

    assert made["categorised"] == 0
    assert _row(out).category_id == _cat(auth_client, "Mortgage")


# --------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------


def test_an_accounts_wording_is_tidied_and_its_type_checked(auth_client: TestClient) -> None:
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    saved = _wording(
        auth_client, card, "  By Authority   To Cc ", "BY AUTHORITY TO CC 0412", "1234"
    )
    assert saved["transfer_wording"] == ["By Authority To Cc"]
    assert saved["bank_identifier"] is None
    bad = auth_client.patch(f"/api/accounts/{card}", json={"type": "piggy_bank"})
    assert bad.status_code == 422
    moved = auth_client.patch(f"/api/accounts/{card}", json={"type": "everyday", "name": "Main"})
    assert (moved.json()["type"], moved.json()["name"]) == ("everyday", "Main")


# --------------------------------------------------------------------------------------
# On update: history from before kinds existed
# --------------------------------------------------------------------------------------


def test_on_update_old_pairs_get_their_kind_and_a_repayment_counts_again(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    internal = _cat(auth_client, "Internal transfers")
    rows = {
        "card_out": _add(auth_client, savings, "2026-06-29", -10000, "X"),
        "card_in": _add(auth_client, card, "2026-06-29", 10000, "Y"),
        "loan_out": _add(auth_client, savings, "2026-06-23", -125492, "P"),
        "loan_in": _add(auth_client, loan, "2026-06-23", 125492, "Q"),
        "orphan": _add(auth_client, savings, "2026-06-02", -500, "R"),
    }
    with SessionLocal() as db:
        # As 0.17 left them: linked, Internal transfers with no score, no kind.
        for group, names in (
            ("g1", ("card_out", "card_in")),
            ("g2", ("loan_out", "loan_in")),
            ("g3", ("orphan",)),
        ):
            for name in names:
                t = db.get(models.Transaction, rows[name])
                assert t is not None
                t.is_transfer, t.transfer_group_id = True, group
                t.category_id, t.confidence = internal, None
        db.execute(select(models.Household)).scalar_one().transfers_classified_at = None
        cash = db.execute(
            select(models.Category).where(models.Category.system_key == "Cash deposit")
        ).scalar_one()
        db.delete(cash)  # a household from before the category existed
        db.commit()

    _classify_transfers_once(engine)

    assert _row(rows["card_out"]).category_id == _cat(auth_client, "Credit-card payments")
    repayment = _row(rows["loan_out"])
    assert (repayment.transfer_kind, repayment.is_transfer) == ("loan_repayment", False)
    assert repayment.category_id == _cat(auth_client, "Mortgage")
    orphan = _row(rows["orphan"])
    assert (orphan.transfer_group_id, orphan.is_transfer, orphan.category_id) == (None, False, None)
    assert _cat(auth_client, "Cash deposit")
    with SessionLocal() as db:
        assert db.execute(select(models.Household.transfers_classified_at)).scalar_one() is not None
    # Once: a later start leaves a person's re-kinding alone.
    group = _row(rows["card_out"]).transfer_group_id
    auth_client.post(f"/api/transfers/{group}/kind", json={"kind": "own_accounts"})
    _classify_transfers_once(engine)
    assert _row(rows["card_out"]).transfer_kind == "own_accounts"


def test_a_household_made_now_is_not_reclassified_later(auth_client: TestClient) -> None:
    with SessionLocal() as db:
        assert db.execute(select(models.Household.transfers_classified_at)).scalar_one() is not None


# --------------------------------------------------------------------------------------
# Report fixes
# --------------------------------------------------------------------------------------


def test_the_forecast_balance_leaves_out_cards_and_loans(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    card = create_account(auth_client, "Visa", "credit_card")["id"]
    _add(auth_client, savings, "2026-06-01", 500000, "OPENING")
    _add(auth_client, loan, "2026-06-01", -45000000, "LOAN DRAWDOWN")
    _add(auth_client, card, "2026-06-01", -120000, "CARD SPEND")

    forecast = auth_client.post("/api/forecast", json={}).json()

    assert forecast["starting_balance_cents"] == 500000


def test_weekly_loan_repayments_are_not_a_duplicate_charge() -> None:
    from app.services.insights import _duplicates

    def txn(day: int, group: str | None) -> models.Transaction:
        return models.Transaction(
            amount_cents=-125492,
            merchant="By Authority To Westpac",
            raw_description="x",
            txn_date=dt.date(2026, 6, day),
            transfer_group_id=group,
        )

    linked = [txn(d, f"g{d}") for d in (2, 9, 16, 23)]
    assert _duplicates(linked) == []
    assert len(_duplicates([txn(d, None) for d in (2, 9)])) == 1


def test_every_insight_alert_type_is_one_the_feed_produces() -> None:
    source = Path(__file__).resolve().parents[1].joinpath("app/services/insights.py").read_text()
    produced = set(re.findall(r'type="([a-z_]+)"', source))
    assert produced >= notifications.INSIGHT_ALERT_TYPES
    assert {"top_mover", "creeping", "fees"} <= notifications.INSIGHT_ALERT_TYPES


# --------------------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------------------


def test_a_note_is_suggested_from_the_description_until_one_is_written(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    txn = _add(
        auth_client, everyday, "2026-06-10", -5000, "OSKO PAYMENT TO JOHN SMITH REF BIRTHDAY GIFT"
    )
    items = auth_client.get("/api/transactions", params={"q": "osko"}).json()["items"]
    assert items[0]["note_suggestion"] == "To John Smith · Birthday Gift"

    auth_client.patch(f"/api/transactions/{txn}", json={"notes": "Sam's 40th"})

    items = auth_client.get("/api/transactions", params={"q": "osko"}).json()["items"]
    assert (items[0]["notes"], items[0]["note_suggestion"]) == ("Sam's 40th", None)


def test_note_suggestions() -> None:
    assert suggest_note("By Authority To Vw") == "To Vw"
    assert suggest_note("TRANSFER FROM EVERYDAY 4412") == "From Everyday"
    assert suggest_note("Payment to Jane - school fees") == "To Jane · school fees"
    assert suggest_note("WOOLWORTHS 1234 SYDNEY") is None
    assert suggest_note(None) is None


def test_a_deposit_waiting_in_review_is_not_called_a_cash_deposit(auth_client: TestClient) -> None:
    # It may be the other half of a transfer: the person decides first.
    everyday = create_account(auth_client, "Everyday", "everyday")["id"]
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, everyday, "2026-06-12", -50000, "PAYMENT")
    deposit = _add(auth_client, savings, "2026-06-12", 50000, "DEPOSIT")

    scan = _scan(auth_client)

    assert (scan["to_review"], scan["cash_deposits"]) == (1, 0)
    assert _row(deposit).category_id is None


def test_rules_may_not_change_either_side_of_a_linked_pair() -> None:
    from app.services.categorise import rules_may_change

    paying_side = models.Transaction(
        amount_cents=-125492,
        is_transfer=False,
        category_locked=False,
        transfer_group_id="g1",
        category_id="mortgage",
        confidence=ML_MAX_CONFIDENCE,
    )
    assert not rules_may_change(paying_side)
    paying_side.transfer_group_id = None
    assert rules_may_change(paying_side)
