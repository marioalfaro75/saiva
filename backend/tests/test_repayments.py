"""Home loan repayments: the regular one and extra ones (0.18.1).

Asked for: "If it is a weekly amount similar to last week's then it is a regular home loan
repayment; if it is a second payment, an unusual amount very different from the regular
weekly repayments, then it is an extra home loan repayment." Both count as spending; the
regular one is a bill. The household sets its regular repayment (amount, how often, from
when) on the loan's account -- Saiva suggests one -- and the sorting follows it.
"""

from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_transfer_kinds import _add, _cat, _row, _scan, _set, _wording

from app import models
from app.db import SessionLocal, engine
from app.dbinit import _classify_transfers_once, _rename_extra_repayments
from app.services.categorise import PERSON_CONFIDENCE
from app.services.repayments import Period, sort_legs, suggest

WEEKLY = 125492
START = dt.date(2026, 6, 5)  # a Friday
EVERY_WEEK = [Period(None, WEEKLY, "weekly")]


def _leg(day: int, cents: int, account: str = "a", n: list[int] = [0]) -> models.Transaction:  # noqa: B006
    n[0] += 1
    return models.Transaction(
        id=f"r{n[0]:04d}",
        account_id=account,
        txn_date=START + dt.timedelta(days=day),
        amount_cents=-cents,
    )


def _extras(
    legs: list[models.Transaction],
    schedule: list[Period] = EVERY_WEEK,
    fixed: dict[str, bool] | None = None,
) -> list[bool]:
    flags = sort_legs(legs, schedule, fixed or {})
    return [flags[t.id] for t in legs]


# --------------------------------------------------------------------------------------
# Sorting by the household's regular repayment
# --------------------------------------------------------------------------------------


def test_the_weekly_amount_is_regular_and_a_lump_sum_between_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(10, 500000), _leg(14, WEEKLY)]
    assert _extras(legs) == [False, False, True, False]


def test_a_second_payment_of_the_usual_amount_days_later_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(9, WEEKLY), _leg(14, WEEKLY)]
    assert _extras(legs) == [False, False, True, False]


def test_a_small_difference_is_still_regular_and_a_large_one_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, int(WEEKLY * 1.08)), _leg(14, int(WEEKLY * 1.25))]
    assert _extras(legs) == [False, False, True]


def test_late_early_moved_and_missed_repayments_are_regular() -> None:
    # Friday, then held over a holiday to Tuesday, then Friday; a missed week; the
    # payment day moved to Monday; a day early.
    days = [0, 7, 18, 21, 35, 38, 45, 51]
    assert _extras([_leg(d, WEEKLY) for d in days]) == [False] * len(days)


def test_a_repayment_paid_from_two_accounts_on_one_day_is_one_regular_repayment() -> None:
    week = [_leg(0, 100000, "alex"), _leg(0, WEEKLY - 100000, "sam")]
    halves = [_leg(7, WEEKLY // 2, "alex"), _leg(7, WEEKLY - WEEKLY // 2, "sam")]
    assert _extras(week + halves) == [False] * 4


def test_an_extra_paid_the_same_day_is_extra_whatever_the_account_or_order() -> None:
    same_account = [_leg(0, 20000), _leg(0, WEEKLY)]
    with_partners = [
        _leg(7, 300000, "alex"),
        _leg(7, 100000, "alex"),
        _leg(7, WEEKLY - 100000, "sam"),
    ]
    assert _extras(same_account + with_partners) == [True, False, True, False, False]


def test_a_persons_choice_is_kept_and_the_rest_of_the_day_follows_it() -> None:
    legs = [_leg(0, 100000, "alex"), _leg(0, WEEKLY - 100000, "sam"), _leg(7, WEEKLY)]
    fixed = {legs[0].id: False}  # a person said Alex's part is the regular repayment
    assert _extras(legs, fixed=fixed) == [False, False, False]
    assert _extras(legs, fixed={legs[2].id: True}) == [False, False, True]


def test_monthly_repayments_moved_for_weekends_stay_regular() -> None:
    dates = ["2019-01-31", "2019-02-28", "2019-03-29", "2019-04-30", "2019-05-31", "2019-06-28"]
    legs = [
        models.Transaction(
            id=f"m{i}", account_id="a", txn_date=dt.date.fromisoformat(d), amount_cents=-543800
        )
        for i, d in enumerate(dates)
    ]
    assert _extras(legs, [Period(None, 543800, "monthly")]) == [False] * 6


def test_a_new_amount_from_a_date_keeps_the_old_ones_regular_before_it() -> None:
    raised = int(WEEKLY * 1.3)
    schedule = [
        Period(None, WEEKLY, "weekly"),
        Period(START + dt.timedelta(days=14), raised, "weekly"),
    ]
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(14, raised), _leg(21, raised), _leg(28, WEEKLY)]
    assert _extras(legs, schedule) == [False, False, False, False, True]


def test_with_no_regular_repayment_set_only_a_persons_extra_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(3, 500000)]
    assert _extras(legs, []) == [False, False]
    assert _extras(legs, [], {legs[1].id: True}) == [False, True]


# --------------------------------------------------------------------------------------
# The suggestion
# --------------------------------------------------------------------------------------


def test_the_suggestion_is_the_amount_most_repayments_are_near() -> None:
    legs = [_leg(7 * i, WEEKLY) for i in range(6)] + [_leg(10, 500000)]
    found = suggest(legs, START + dt.timedelta(days=40))
    assert found == Period(START, WEEKLY, "weekly")


def test_the_suggestion_follows_a_rate_rise_and_says_since_when() -> None:
    raised = int(WEEKLY * 1.3)
    legs = [_leg(7 * i, WEEKLY) for i in range(4)] + [_leg(28 + 7 * i, raised) for i in range(6)]
    found = suggest(legs, START + dt.timedelta(days=70))
    assert found == Period(START + dt.timedelta(days=28), raised, "weekly")


def test_fortnightly_and_monthly_are_suggested_as_such() -> None:
    fortnightly = [_leg(14 * i, 250985) for i in range(5)]
    assert suggest(fortnightly, START + dt.timedelta(days=60)).every == "fortnightly"  # type: ignore[union-attr]
    monthly = [_leg(30 * i, 543800) for i in range(4)]
    assert suggest(monthly, START + dt.timedelta(days=100)).every == "monthly"  # type: ignore[union-attr]


def test_nothing_is_suggested_from_too_little() -> None:
    assert suggest([_leg(0, WEEKLY), _leg(7, WEEKLY)], START + dt.timedelta(days=10)) is None


# --------------------------------------------------------------------------------------
# In the app
# --------------------------------------------------------------------------------------


def _weeks(
    client: TestClient, *amounts_by_day: tuple[int, int], schedule: bool = True
) -> tuple[str, str, list[str]]:
    savings = create_account(client, "Savings", "savings")["id"]
    loan = create_account(client, "Westpac home loan", "home_loan")["id"]
    _wording(client, loan, "By Authority To Westpac")
    start = dt.date.today() - dt.timedelta(days=35)
    paid = []
    for day, cents in amounts_by_day:
        date = (start + dt.timedelta(days=day)).isoformat()
        paid.append(_add(client, savings, date, -cents, "By Authority To Westpac"))
        _add(client, loan, date, cents, "REPAYMENT")
    _scan(client)
    if schedule:
        _set_repayment(client, loan, WEEKLY, "weekly")
    return savings, loan, paid


def _set_repayment(
    client: TestClient, loan: str, cents: int, every: str, since: str | None = None
) -> dict:
    resp = client.patch(
        f"/api/accounts/{loan}",
        json={"repayment_schedule": [{"since": since, "cents": cents, "every": every}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _repayment_of(client: TestClient, ids: list[str], account: str | None = None) -> list[str]:
    params: dict = {"page_size": 100}
    if account:
        params["account_id"] = account
    listed = {
        t["id"]: t["repayment"]
        for t in client.get("/api/transactions", params=params).json()["items"]
    }
    return [listed[i] for i in ids]


def test_setting_the_regular_repayment_files_the_extras_and_both_count(
    auth_client: TestClient,
) -> None:
    _, _, paid = _weeks(
        auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY), schedule=False
    )
    mortgage, extra = _cat(auth_client, "Mortgage"), _cat(auth_client, "Extra repayments")
    # Not set yet: every repayment is regular, as before.
    assert {_row(t).category_id for t in paid} == {mortgage}

    loan = next(
        a["id"] for a in auth_client.get("/api/accounts").json() if a["type"] == "home_loan"
    )
    _set_repayment(auth_client, loan, WEEKLY, "weekly")

    assert [_row(t).category_id for t in paid] == [mortgage, mortgage, extra, mortgage]
    assert all(not _row(t).is_transfer for t in paid)
    assert _repayment_of(auth_client, paid) == ["regular", "regular", "extra", "regular"]
    summary = auth_client.get(
        "/api/dashboard/summary",
        params={
            "period": "custom",
            "start": (dt.date.today() - dt.timedelta(days=40)).isoformat(),
            "end": dt.date.today().isoformat(),
        },
    ).json()
    assert summary["expense_cents"] == 3 * WEEKLY + 500000


def test_clearing_the_regular_repayment_files_everything_as_regular_again(
    auth_client: TestClient,
) -> None:
    _, loan, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))
    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")

    resp = auth_client.patch(f"/api/accounts/{loan}", json={"repayment_schedule": []})

    assert resp.json()["repayment_schedule"] == []
    assert _row(paid[2]).category_id == _cat(auth_client, "Mortgage")


def test_a_suggestion_is_offered_until_one_is_set_and_again_when_it_moves(
    auth_client: TestClient,
) -> None:
    _, loan, _ = _weeks(auth_client, *[(7 * i, WEEKLY) for i in range(5)], schedule=False)

    offered = auth_client.get(f"/api/accounts/{loan}").json()["repayment_suggestion"]
    assert (offered["cents"], offered["every"]) == (WEEKLY, "weekly")
    saved = _set_repayment(auth_client, loan, offered["cents"], offered["every"], offered["since"])
    assert saved["repayment_suggestion"] is None
    assert saved["repayment_schedule"][0]["cents"] == WEEKLY


def test_the_regular_repayment_is_kept_oldest_first_one_a_date(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Westpac home loan", "home_loan")["id"]
    resp = auth_client.patch(
        f"/api/accounts/{loan}",
        json={
            "repayment_schedule": [
                {"since": "2026-06-01", "cents": 140000, "every": "weekly"},
                {"since": None, "cents": WEEKLY, "every": "weekly"},
                {"since": "2026-06-01", "cents": 150000, "every": "weekly"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert [(p["since"], p["cents"]) for p in resp.json()["repayment_schedule"]] == [
        (None, WEEKLY),
        ("2026-06-01", 150000),
    ]
    bad = auth_client.patch(
        f"/api/accounts/{loan}",
        json={"repayment_schedule": [{"since": None, "cents": 0, "every": "weekly"}]},
    )
    assert bad.status_code == 422


def test_a_person_can_say_a_repayment_is_extra_or_regular(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (14, WEEKLY))
    group = _row(paid[1]).transfer_group_id

    resp = auth_client.post(f"/api/transfers/{group}/repayment", json={"extra": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["outflow"]["repayment"] == "extra"
    assert resp.json()["inflow"]["repayment"] == "extra"
    row = _row(paid[1])
    assert (row.category_id, row.confidence) == (
        _cat(auth_client, "Extra repayments"),
        PERSON_CONFIDENCE,
    )
    _scan(auth_client)
    assert _row(paid[1]).category_id == _cat(auth_client, "Extra repayments")
    back = auth_client.post(f"/api/transfers/{group}/repayment", json={"extra": False})
    assert back.json()["outflow"]["repayment"] == "regular"


def test_only_a_home_loans_repayments_are_regular_or_extra(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    car = create_account(auth_client, "Car loan", "personal_loan")["id"]
    out = _add(auth_client, savings, "2026-06-05", -45000, "TRANSFER TO LOAN")
    _add(auth_client, car, "2026-06-05", 45000, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    group = _row(out).transfer_group_id

    resp = auth_client.post(f"/api/transfers/{group}/repayment", json={"extra": True})

    assert resp.status_code == 400
    assert _row(out).category_id == _cat(auth_client, "Loan repayments")
    missing = auth_client.post("/api/transfers/nope/repayment", json={"extra": True})
    assert missing.status_code == 404


def test_a_persons_or_a_locked_category_is_left_alone_and_a_rules_is_sorted(
    auth_client: TestClient,
) -> None:
    _, loan, paid = _weeks(
        auth_client,
        (0, WEEKLY),
        (7, WEEKLY),
        (10, 500000),
        (14, WEEKLY),
        (17, 400000),
        (21, 300000),
        schedule=False,
    )
    mortgage = _cat(auth_client, "Mortgage")
    _set(paid[2], category_id=mortgage, confidence=0.99)  # a rule's, from before linking
    _set(paid[4], category_id=mortgage, category_locked=True)
    gifts = _cat(auth_client, "Gifts")
    _set(paid[5], category_id=gifts, confidence=PERSON_CONFIDENCE)

    _set_repayment(auth_client, loan, WEEKLY, "weekly")

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")
    assert _row(paid[4]).category_id == mortgage
    assert _row(paid[5]).category_id == gifts


def test_the_loans_side_is_filed_to_match(auth_client: TestClient) -> None:
    _, loan, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY))

    listed = auth_client.get(
        "/api/transactions", params={"account_id": loan, "page_size": 50}
    ).json()["items"]

    by_amount = {t["amount_cents"]: (t["repayment"], t["category_name"]) for t in listed}
    assert by_amount == {
        WEEKLY: ("regular", "Mortgage"),
        500000: ("extra", "Extra repayments"),
    }
    del paid


def test_changing_a_home_loan_to_a_personal_loan_moves_extras_to_loan_repayments(
    auth_client: TestClient,
) -> None:
    _, loan, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))

    auth_client.patch(f"/api/accounts/{loan}", json={"type": "personal_loan"})

    assert {_row(t).category_id for t in paid} == {_cat(auth_client, "Loan repayments")}


def test_on_start_repayments_are_sorted_by_the_regular_repayment(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))
    _set(paid[2], category_id=_cat(auth_client, "Mortgage"))  # as 0.18.0 left it

    _classify_transfers_once(engine)

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")


# --------------------------------------------------------------------------------------
# Bills
# --------------------------------------------------------------------------------------


def _series(client: TestClient) -> list[dict]:
    return client.get("/api/recurring").json()["series"]


def test_with_a_regular_repayment_set_the_bill_is_the_loans(auth_client: TestClient) -> None:
    _weeks(auth_client, *[(7 * i, WEEKLY) for i in range(6)], (10, 500000))

    series = _series(auth_client)

    assert [s["merchant"] for s in series] == ["Westpac home loan repayment"]
    bill = series[0]
    assert (bill["cadence"], bill["typical_amount_cents"], bill["category_name"]) == (
        "weekly",
        WEEKLY,
        "Mortgage",
    )
    assert bill["occurrences"] == 6
    upcoming = auth_client.get("/api/recurring/upcoming", params={"days": 14}).json()["bills"]
    assert any(b["merchant"] == "Westpac home loan repayment" for b in upcoming)


def test_without_one_the_repayments_are_a_bill_by_description_as_before(
    auth_client: TestClient,
) -> None:
    _weeks(auth_client, *[(7 * i, WEEKLY) for i in range(6)], schedule=False)

    assert [s["merchant"] for s in _series(auth_client)] == ["By Authority To Westpac"]


def test_the_bill_counts_debits_the_loan_has_no_statement_for_yet(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    start = dt.date.today() - dt.timedelta(days=63)
    for week in range(10):
        date = (start + dt.timedelta(days=7 * week)).isoformat()
        _add(auth_client, savings, date, -WEEKLY, "By Authority To Westpac")
        if 4 <= week <= 8:
            _add(auth_client, loan, date, WEEKLY, "REPAYMENT")
    _scan(auth_client)
    _set_repayment(auth_client, loan, WEEKLY, "weekly")

    series = _series(auth_client)

    assert [s["merchant"] for s in series] == ["Westpac home loan repayment"]
    assert series[0]["occurrences"] == 10
    assert series[0]["last_date"] == (start + dt.timedelta(days=63)).isoformat()


def test_income_and_other_bills_by_the_same_description_are_left_alone(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    start = dt.date.today() - dt.timedelta(days=42)
    for week in range(6):
        date = (start + dt.timedelta(days=7 * week)).isoformat()
        _add(auth_client, everyday, date, -WEEKLY, "TRANSFER TO HOME LOAN")
        _add(auth_client, loan, date, WEEKLY, "TRANSFER FROM EVERYDAY")
        _add(auth_client, everyday, date, 22000, "TRANSFER FROM BOARDER")
        _add(auth_client, everyday, date, -8000, "TRANSFER TO HOME LOAN")  # a different bill
    _scan(auth_client)
    _set_repayment(auth_client, loan, WEEKLY, "weekly")

    merchants = {s["merchant"]: s for s in _series(auth_client)}

    assert "Home loan repayment" in merchants
    assert any(s["direction"] == "income" for s in merchants.values())
    assert merchants["Home loan repayment"]["occurrences"] == 6
    # The smaller one by the same description is its own bill, not part of the loan's.
    assert any(s["typical_amount_cents"] == 8000 for s in merchants.values())


def test_a_personal_loans_bill_is_loan_repayments(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    car = create_account(auth_client, "Car loan", "personal_loan")["id"]
    start = dt.date.today() - dt.timedelta(days=61)
    for month in range(3):
        date = (start + dt.timedelta(days=30 * month)).isoformat()
        _add(auth_client, savings, date, -45000, "TRANSFER TO LOAN")
        _add(auth_client, car, date, 45000, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    _set_repayment(auth_client, car, 45000, "monthly")

    bill = next(s for s in _series(auth_client) if s["merchant"] == "Car loan repayment")

    assert bill["category_name"] == "Loan repayments"


# --------------------------------------------------------------------------------------
# The category
# --------------------------------------------------------------------------------------


def test_the_old_category_is_renamed_unless_the_household_renamed_it(
    auth_client: TestClient,
) -> None:
    with SessionLocal() as db:
        extra = db.execute(
            select(models.Category).where(models.Category.system_key == "Extra repayments")
        ).scalar_one()
        extra.system_key, extra.name = "Offset/Extra repayments", "Offset/Extra repayments"
        db.commit()
        extra_id = extra.id

    _rename_extra_repayments(engine)

    with SessionLocal() as db:
        renamed = db.get(models.Category, extra_id)
        assert renamed is not None
        assert (renamed.system_key, renamed.name) == ("Extra repayments", "Extra repayments")
        renamed.system_key, renamed.name = "Offset/Extra repayments", "Paying it down"
        db.commit()

    _rename_extra_repayments(engine)

    with SessionLocal() as db:
        kept = db.get(models.Category, extra_id)
        assert kept is not None
        assert (kept.system_key, kept.name) == ("Extra repayments", "Paying it down")


def test_the_rename_never_makes_a_name_that_reads_the_same_twice(auth_client: TestClient) -> None:
    with SessionLocal() as db:
        extra = db.execute(
            select(models.Category).where(models.Category.system_key == "Extra repayments")
        ).scalar_one()
        extra.system_key, extra.name = "Offset/Extra repayments", "Offset/Extra repayments"
        db.commit()
        extra_id = extra.id
    resp = auth_client.post("/api/categories", json={"name": "Extra Repayments", "kind": "expense"})
    assert resp.status_code in (200, 201), resp.text

    _rename_extra_repayments(engine)

    with SessionLocal() as db:
        kept = db.get(models.Category, extra_id)
        assert kept is not None
        assert (kept.system_key, kept.name) == ("Extra repayments", "Offset/Extra repayments")


def test_no_regular_or_extra_choice_when_its_category_is_gone(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY))
    for name in ("Extra repayments", "Mortgage"):
        assert auth_client.delete(f"/api/categories/{_cat(auth_client, name)}").status_code == 200
        assert set(_repayment_of(auth_client, paid)) == {None}
    group = _row(paid[0]).transfer_group_id
    resp = auth_client.post(f"/api/transfers/{group}/repayment", json={"extra": True})
    assert resp.status_code == 409
    assert "Extra repayments" in resp.json()["detail"]


# --------------------------------------------------------------------------------------
# The edges
# --------------------------------------------------------------------------------------


def _at(id_: str, day: int, cents: int, account: str = "a") -> models.Transaction:
    return models.Transaction(
        id=id_, account_id=account, txn_date=START + dt.timedelta(days=day), amount_cents=-cents
    )


def test_of_two_near_the_amount_on_one_day_the_nearer_is_regular() -> None:
    legs = [_at("a-near-ish", 0, int(WEEKLY * 1.05)), _at("z-exact", 0, WEEKLY)]
    assert _extras(legs) == [True, False]


def test_a_payment_soon_after_one_a_person_called_regular_is_extra() -> None:
    legs = [_at("p1", 0, WEEKLY), _at("p2", 2, WEEKLY)]
    assert _extras(legs, fixed={"p1": False}) == [False, True]


def test_the_suggestion_reaches_back_past_the_recent_months() -> None:
    legs = [_leg(7 * i, WEEKLY) for i in range(45)]
    found = suggest(legs, START + dt.timedelta(days=7 * 44 + 1))
    assert found is not None and found.since == START


def test_a_new_suggestion_when_the_repayments_move_away_from_the_one_set(
    auth_client: TestClient,
) -> None:
    raised = int(WEEKLY * 1.3)
    _, loan, _ = _weeks(
        auth_client, *[(7 * i, WEEKLY) for i in range(2)], *[(14 + 7 * i, raised) for i in range(4)]
    )

    offered = auth_client.get(f"/api/accounts/{loan}").json()["repayment_suggestion"]

    assert offered is not None
    assert offered["cents"] == raised


def test_no_suggestion_for_a_personal_loan(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    car = create_account(auth_client, "Car loan", "personal_loan")["id"]
    start = dt.date.today() - dt.timedelta(days=35)
    for week in range(5):
        date = (start + dt.timedelta(days=7 * week)).isoformat()
        _add(auth_client, savings, date, -45000, "TRANSFER TO LOAN")
        _add(auth_client, car, date, 45000, "TRANSFER FROM SAVINGS")
    _scan(auth_client)

    assert auth_client.get(f"/api/accounts/{car}").json()["repayment_suggestion"] is None


def test_a_locked_loan_side_stays_as_it_is(auth_client: TestClient) -> None:
    _, loan, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), schedule=False)
    lump_side = next(
        t["id"]
        for t in auth_client.get("/api/transactions", params={"account_id": loan}).json()["items"]
        if t["amount_cents"] == 500000
    )
    _set(lump_side, category_locked=True)

    _set_repayment(auth_client, loan, WEEKLY, "weekly")

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")
    assert _row(lump_side).category_id == _cat(auth_client, "Mortgage")


def test_a_debit_with_the_loans_wording_from_any_account_counts_with_its_bill(
    auth_client: TestClient,
) -> None:
    _, loan, _ = _weeks(auth_client, *[(7 * i, WEEKLY) for i in range(4)])
    everyday = create_account(auth_client, "Everyday")["id"]
    _add(
        auth_client, everyday, dt.date.today().isoformat(), -WEEKLY, "BPAY By Authority To Westpac"
    )

    bill = next(s for s in _series(auth_client) if s["merchant"] == "Westpac home loan repayment")

    assert bill["occurrences"] == 5
    assert bill["last_date"] == dt.date.today().isoformat()
    del loan
