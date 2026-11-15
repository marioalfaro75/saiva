"""Home loan repayments: the regular one and extra ones (0.18.1).

Asked for: "If it is a weekly amount similar to last week's then it is a regular home loan
repayment; if it is a second payment, an unusual amount very different from the regular
weekly repayments, then it is an extra home loan repayment." Both count as spending; the
regular one is a bill.
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
from app.services.repayments import sort_out

WEEKLY = 125492


def _leg(day: int, cents: int, n: list[int] = [0]) -> models.Transaction:  # noqa: B006
    n[0] += 1
    return models.Transaction(
        id=f"r{n[0]}", txn_date=dt.date(2026, 6, 1) + dt.timedelta(days=day), amount_cents=-cents
    )


def _extras(legs: list[models.Transaction], fixed: dict[str, bool] | None = None) -> list[bool]:
    flags = sort_out(legs, fixed)
    return [flags[t.id] for t in legs]


# --------------------------------------------------------------------------------------
# The pattern
# --------------------------------------------------------------------------------------


def test_the_weekly_amount_is_regular_and_a_lump_sum_between_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(10, 500000), _leg(14, WEEKLY), _leg(21, WEEKLY)]
    assert _extras(legs) == [False, False, True, False, False]


def test_a_second_payment_of_the_usual_amount_in_the_same_week_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(9, WEEKLY), _leg(14, WEEKLY)]
    assert _extras(legs) == [False, False, True, False]


def test_a_small_change_in_the_amount_is_still_regular() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(14, int(WEEKLY * 1.08))]
    assert _extras(legs) == [False, False, False]


def test_a_new_amount_twice_in_a_row_is_the_new_usual() -> None:
    raised = int(WEEKLY * 1.3)  # after a rate rise
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(14, raised), _leg(21, raised), _leg(28, raised)]
    assert _extras(legs) == [False, False, False, False, False]


def test_a_one_off_amount_on_the_usual_day_stays_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(14, 300000), _leg(21, WEEKLY)]
    assert _extras(legs) == [False, False, True, False]


def test_a_missed_week_does_not_make_the_next_one_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(21, WEEKLY)]
    assert _extras(legs) == [False, False, False]


def test_fortnightly_repayments_have_their_own_period() -> None:
    legs = [_leg(0, WEEKLY), _leg(14, WEEKLY), _leg(28, WEEKLY), _leg(35, WEEKLY), _leg(42, WEEKLY)]
    # A week into a fortnight: a second payment in the same period.
    assert _extras(legs) == [False, False, False, True, False]


def test_a_persons_decision_is_kept_and_teaches_the_usual() -> None:
    raised = int(WEEKLY * 1.3)
    legs = [_leg(0, WEEKLY), _leg(7, raised), _leg(14, raised)]
    fixed = {legs[1].id: False}  # the person says: that was the regular one
    assert _extras(legs, fixed) == [False, False, False]
    assert _extras(legs, {legs[0].id: True}) == [True, False, False]


# --------------------------------------------------------------------------------------
# In the app
# --------------------------------------------------------------------------------------


def _weeks(client: TestClient, *amounts_by_day: tuple[int, int]) -> tuple[str, str, list[str]]:
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
    return savings, loan, paid


def test_an_extra_repayment_is_filed_as_extra_and_both_count_as_spending(
    auth_client: TestClient,
) -> None:
    _, _, paid = _weeks(
        auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY), (21, WEEKLY)
    )

    mortgage, extra = _cat(auth_client, "Mortgage"), _cat(auth_client, "Extra repayments")
    assert [_row(t).category_id for t in paid] == [mortgage, mortgage, extra, mortgage, mortgage]
    assert all(not _row(t).is_transfer for t in paid)
    listed = {
        t["id"]: t["repayment"]
        for t in auth_client.get("/api/transactions", params={"page_size": 50}).json()["items"]
    }
    assert [listed[t] for t in paid] == ["regular", "regular", "extra", "regular", "regular"]


def test_a_person_can_say_a_repayment_is_extra_or_regular(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (14, WEEKLY))
    group = _row(paid[1]).transfer_group_id

    resp = auth_client.post(f"/api/transfers/{group}/repayment", json={"extra": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["outflow"]["repayment"] == "extra"
    row = _row(paid[1])
    assert (row.category_id, row.confidence) == (
        _cat(auth_client, "Extra repayments"),
        PERSON_CONFIDENCE,
    )
    # Kept through a scan.
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


def test_a_rule_or_a_persons_category_on_a_repayment_is_left_alone(
    auth_client: TestClient,
) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))
    gifts = _cat(auth_client, "Gifts")
    _set(paid[2], category_id=gifts, confidence=PERSON_CONFIDENCE)
    _set(paid[1], category_locked=True)

    _scan(auth_client)

    assert _row(paid[2]).category_id == gifts
    assert _row(paid[1]).category_id == _cat(auth_client, "Mortgage")


def test_the_regular_repayment_is_a_bill_and_an_extra_is_not(auth_client: TestClient) -> None:
    _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY), (21, WEEKLY),
           (28, WEEKLY), (35, WEEKLY))

    series = auth_client.get("/api/recurring").json()["series"]
    loans = [s for s in series if s["merchant"] == "Westpac home loan repayment"]
    assert len(loans) == 1
    assert (loans[0]["cadence"], loans[0]["typical_amount_cents"]) == ("weekly", WEEKLY)
    assert loans[0]["category_name"] == "Mortgage"
    assert not any("authority" in s["merchant"].lower() for s in series)
    upcoming = auth_client.get("/api/recurring/upcoming", params={"days": 14}).json()["bills"]
    assert any(b["merchant"] == "Westpac home loan repayment" for b in upcoming)


def test_changing_a_home_loan_to_a_personal_loan_moves_extras_to_loan_repayments(
    auth_client: TestClient,
) -> None:
    _, loan, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))
    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")

    auth_client.patch(f"/api/accounts/{loan}", json={"type": "personal_loan"})

    assert {_row(t).category_id for t in paid} == {_cat(auth_client, "Loan repayments")}


# --------------------------------------------------------------------------------------
# On update
# --------------------------------------------------------------------------------------


def test_on_update_repayments_already_linked_are_sorted(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))
    mortgage = _cat(auth_client, "Mortgage")
    _set(paid[2], category_id=mortgage)  # as 0.18.0 left it

    _classify_transfers_once(engine)

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")


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


# --------------------------------------------------------------------------------------
# The edges
# --------------------------------------------------------------------------------------


def test_an_amount_a_quarter_off_on_the_usual_day_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(14, int(WEEKLY * 1.25)), _leg(21, WEEKLY)]
    assert _extras(legs) == [False, False, True, False]


def test_a_new_amount_twice_in_one_week_is_not_the_new_usual() -> None:
    raised = int(WEEKLY * 1.3)
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(14, raised), _leg(16, raised)]
    assert _extras(legs) == [False, False, True, True]


def test_a_lump_sum_between_repayments_is_not_half_of_a_new_usual() -> None:
    lump = WEEKLY * 3
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(10, lump), _leg(17, lump)]
    assert _extras(legs) == [False, False, True, True]


def test_only_what_saiva_filed_is_moved(auth_client: TestClient) -> None:
    _, _, paid = _weeks(
        auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY), (17, 400000)
    )
    mortgage = _cat(auth_client, "Mortgage")
    _set(paid[2], category_id=mortgage, confidence=0.99)  # a rule's
    _set(paid[4], category_id=mortgage, category_locked=True)
    guessed = _cat(auth_client, "Gifts")
    _set(paid[3], category_id=guessed, confidence=0.98)

    _scan(auth_client)

    assert _row(paid[2]).category_id == mortgage
    assert _row(paid[4]).category_id == mortgage
    assert _row(paid[3]).category_id == guessed


def test_the_bill_is_only_the_regular_repayments(auth_client: TestClient) -> None:
    _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY), (21, WEEKLY),
           (28, WEEKLY), (35, WEEKLY))

    series = auth_client.get("/api/recurring").json()["series"]

    loan = next(s for s in series if s["merchant"] == "Westpac home loan repayment")
    assert loan["occurrences"] == 6


def test_steady_repayments_are_one_bill_not_two(auth_client: TestClient) -> None:
    _weeks(auth_client, *[(7 * i, WEEKLY) for i in range(6)])

    series = auth_client.get("/api/recurring").json()["series"]

    assert [s["merchant"] for s in series] == ["Westpac home loan repayment"]


def test_making_a_pair_a_loan_repayment_sorts_it(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000))
    group = _row(paid[2]).transfer_group_id
    auth_client.post(f"/api/transfers/{group}/kind", json={"kind": "own_accounts"})

    auth_client.post(f"/api/transfers/{group}/kind", json={"kind": "loan_repayment"})

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")


def test_a_loan_corrected_to_a_home_loan_has_its_extras_sorted(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac", "personal_loan")["id"]
    start = dt.date.today() - dt.timedelta(days=21)
    paid = []
    for day, cents in ((0, WEEKLY), (7, WEEKLY), (10, 500000)):
        date = (start + dt.timedelta(days=day)).isoformat()
        paid.append(_add(auth_client, savings, date, -cents, "TRANSFER TO LOAN"))
        _add(auth_client, loan, date, cents, "TRANSFER FROM SAVINGS")
    _scan(auth_client)

    auth_client.patch(f"/api/accounts/{loan}", json={"type": "home_loan"})

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")
    assert _row(paid[1]).category_id == _cat(auth_client, "Mortgage")
