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
    legs = [_leg(0, WEEKLY), _leg(14, WEEKLY), _leg(28, WEEKLY), _leg(31, WEEKLY), _leg(42, WEEKLY)]
    # Three days after a fortnightly repayment: a second payment in the same fortnight.
    assert _extras(legs) == [False, False, False, True, False]


def test_a_fortnightly_payers_extra_amount_a_week_in_is_extra() -> None:
    legs = [_leg(0, WEEKLY), _leg(14, WEEKLY), _leg(21, 300000), _leg(28, WEEKLY), _leg(42, WEEKLY)]
    assert _extras(legs) == [False, False, True, False, False]


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


def test_a_new_amount_a_week_apart_after_the_old_stops_is_the_new_usual() -> None:
    bigger = WEEKLY * 3
    legs = [_leg(0, WEEKLY), _leg(7, WEEKLY), _leg(10, bigger), _leg(17, bigger)]
    assert _extras(legs) == [False, False, False, False]


def test_a_lump_sum_twice_while_the_repayments_go_on_is_extra() -> None:
    lump = WEEKLY * 3
    legs = [_leg(d, WEEKLY) for d in (0, 7, 14, 21, 28)] + [_leg(10, lump), _leg(17, lump)]
    flags = sort_out(legs)
    assert [flags[t.id] for t in legs] == [False] * 5 + [True, True]


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


# --------------------------------------------------------------------------------------
# What the review of 0.18.1 found
# --------------------------------------------------------------------------------------


def _run(start: int, every: int, count: int, cents: int) -> list[models.Transaction]:
    return [_leg(start + every * i, cents) for i in range(count)]


def test_moving_from_monthly_to_fortnightly_makes_the_fortnightly_one_the_usual() -> None:
    legs = _run(0, 30, 5, 543800) + _run(134, 14, 10, 250985)
    assert _extras(legs) == [False] * 15


def test_moving_from_fortnightly_to_weekly_makes_the_weekly_one_the_usual() -> None:
    legs = _run(0, 14, 6, 250985) + _run(77, 7, 10, WEEKLY)
    assert _extras(legs) == [False] * 16


def test_a_missed_week_early_on_does_not_make_every_other_week_extra() -> None:
    legs = [_leg(0, WEEKLY)] + _run(14, 7, 8, WEEKLY)
    assert _extras(legs) == [False] * 9


def test_a_standing_extra_stays_extra_through_a_rate_rise() -> None:
    raised = int(WEEKLY * 1.15)
    regular = _run(0, 7, 6, WEEKLY) + _run(42, 7, 6, raised)
    standing = _run(4, 7, 12, 20000)
    flags = sort_out(regular + standing)
    assert [flags[t.id] for t in regular] == [False] * 12
    assert [flags[t.id] for t in standing] == [True] * 12


def test_a_lump_between_the_first_two_at_a_new_amount_does_not_stop_the_change() -> None:
    raised = int(WEEKLY * 1.15)
    legs = _run(0, 7, 6, WEEKLY) + [_leg(42, raised), _leg(45, 500000)] + _run(49, 7, 3, raised)
    assert _extras(legs) == [False] * 6 + [False, True] + [False] * 3


def test_repayments_pushed_back_by_public_holidays_stay_regular() -> None:
    days = ["2026-12-04", "2026-12-11", "2026-12-18", "2026-12-29", "2027-01-04", "2027-01-08",
            "2027-01-15"]
    legs = [
        models.Transaction(id=f"h{i}", txn_date=dt.date.fromisoformat(d), amount_cents=-WEEKLY)
        for i, d in enumerate(days)
    ]
    assert _extras(legs) == [False] * 7


def test_a_repayment_paid_from_two_accounts_on_one_day_is_one_regular_repayment() -> None:
    legs: list[models.Transaction] = []
    for week in range(5):
        legs += [_leg(7 * week, 100000), _leg(7 * week, WEEKLY - 100000)]
    assert _extras(legs) == [False] * 10
    halves: list[models.Transaction] = []
    for week in range(5):
        halves += [_leg(7 * week, WEEKLY // 2), _leg(7 * week, WEEKLY - WEEKLY // 2)]
    assert _extras(halves) == [False] * 10


def test_an_extra_paid_the_same_day_as_the_repayment_is_extra_whatever_its_id() -> None:
    for lump_id in ("a-first", "z-last"):
        legs = _run(0, 7, 4, WEEKLY)
        lump = models.Transaction(
            id=lump_id, txn_date=legs[2].txn_date, amount_cents=-300000
        )
        flags = sort_out([*legs, lump])
        assert flags[lump.id] is True
        assert [flags[t.id] for t in legs] == [False] * 4


def test_a_lump_at_the_start_of_the_history_is_extra() -> None:
    legs = [_leg(0, 5000000)] + _run(2, 7, 4, WEEKLY)
    assert _extras(legs) == [True, False, False, False, False]


def test_a_rules_mortgage_from_before_linking_is_sorted_too(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    mortgage = _cat(auth_client, "Mortgage")
    start = dt.date.today() - dt.timedelta(days=30)
    paid = []
    for day, cents in ((0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY), (21, WEEKLY)):
        date = (start + dt.timedelta(days=day)).isoformat()
        out = _add(auth_client, savings, date, -cents, "By Authority To Westpac")
        _set(out, category_id=mortgage, confidence=0.99)  # a rule filed it at import
        paid.append(out)
        _add(auth_client, loan, date, cents, "REPAYMENT")

    _scan(auth_client)

    assert _row(paid[2]).category_id == _cat(auth_client, "Extra repayments")


def test_the_rename_never_makes_a_name_that_reads_the_same_twice(auth_client: TestClient) -> None:
    with SessionLocal() as db:  # as an older release left it
        extra = db.execute(
            select(models.Category).where(models.Category.system_key == "Extra repayments")
        ).scalar_one()
        extra.system_key, extra.name = "Offset/Extra repayments", "Offset/Extra repayments"
        db.commit()
        extra_id = extra.id
    resp = auth_client.post(
        "/api/categories", json={"name": "Extra Repayments", "kind": "expense"}
    )
    assert resp.status_code in (200, 201), resp.text

    _rename_extra_repayments(engine)

    with SessionLocal() as db:
        kept = db.get(models.Category, extra_id)
        assert kept is not None
        assert (kept.system_key, kept.name) == ("Extra repayments", "Offset/Extra repayments")


def _bill(client: TestClient, name: str = "Westpac home loan repayment") -> dict:
    series = client.get("/api/recurring").json()["series"]
    found = [s for s in series if s["merchant"] == name]
    assert len(found) == 1, [s["merchant"] for s in series]
    return found[0]


def test_debits_not_linked_yet_count_with_the_loans_bill_not_as_another(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    start = dt.date.today() - dt.timedelta(days=63)
    for week in range(10):
        date = (start + dt.timedelta(days=7 * week)).isoformat()
        _add(auth_client, savings, date, -WEEKLY, "By Authority To Westpac")
        if 4 <= week <= 8:  # the loan's statement covers five of those weeks only
            _add(auth_client, loan, date, WEEKLY, "REPAYMENT")
    _scan(auth_client)

    bill = _bill(auth_client)
    series = auth_client.get("/api/recurring").json()["series"]

    assert len(series) == 1, [s["merchant"] for s in series]
    assert bill["occurrences"] == 10
    assert bill["last_date"] == (start + dt.timedelta(days=63)).isoformat()


def test_repayments_marked_by_hand_into_an_untyped_loan_are_still_a_bill(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    loan = create_account(auth_client, "Westpac loan", "everyday")["id"]
    start = dt.date.today() - dt.timedelta(days=35)
    for week in range(6):
        date = (start + dt.timedelta(days=7 * week)).isoformat()
        out = _add(auth_client, savings, date, -WEEKLY, "TRANSFER TO LOAN")
        _add(auth_client, loan, date, WEEKLY, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    for t in auth_client.get("/api/transfers/recent").json():
        auth_client.post(f"/api/transfers/{t['group_id']}/kind", json={"kind": "loan_repayment"})

    bill = _bill(auth_client, "Westpac loan repayment")

    assert (bill["cadence"], bill["category_name"]) == ("weekly", "Loan repayments")
    del out


def test_a_bill_goes_by_the_pattern_not_by_a_category_a_person_moved(
    auth_client: TestClient,
) -> None:
    _, _, paid = _weeks(auth_client, *[(7 * i, WEEKLY) for i in range(5)], (31, 500000))
    _set(paid[-1], category_id=_cat(auth_client, "Gifts"), confidence=PERSON_CONFIDENCE)

    bill = _bill(auth_client)

    assert bill["occurrences"] == 5
    assert bill["category_name"] == "Mortgage"


def test_a_bill_shows_the_new_amount_after_a_rate_rise(auth_client: TestClient) -> None:
    raised = int(WEEKLY * 1.3)
    _weeks(
        auth_client,
        *[(7 * i, WEEKLY) for i in range(3)],
        *[(21 + 7 * i, raised) for i in range(3)],
    )

    assert _bill(auth_client)["typical_amount_cents"] == raised


def test_a_personal_loans_bill_is_loan_repayments(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    car = create_account(auth_client, "Car loan", "personal_loan")["id"]
    start = dt.date.today() - dt.timedelta(days=61)
    outs = []
    for month in range(3):
        date = (start + dt.timedelta(days=30 * month)).isoformat()
        outs.append(_add(auth_client, savings, date, -45000, "TRANSFER TO LOAN"))
        _add(auth_client, car, date, 45000, "TRANSFER FROM SAVINGS")
    _scan(auth_client)
    _set(outs[-1], category_id=None, confidence=PERSON_CONFIDENCE)

    assert _bill(auth_client, "Car loan repayment")["category_name"] == "Loan repayments"


def test_no_regular_or_extra_choice_when_its_category_is_gone(auth_client: TestClient) -> None:
    _, _, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY))
    extra = _cat(auth_client, "Extra repayments")
    assert auth_client.delete(f"/api/categories/{extra}").status_code == 200
    group = _row(paid[0]).transfer_group_id

    listed = auth_client.get("/api/transactions", params={"page_size": 50}).json()["items"]
    resp = auth_client.post(f"/api/transfers/{group}/repayment", json={"extra": True})

    assert {t["repayment"] for t in listed} == {None}
    assert resp.status_code == 409
    assert "Extra repayments" in resp.json()["detail"]


def test_the_loans_side_shows_regular_or_extra_too(auth_client: TestClient) -> None:
    _, loan, paid = _weeks(auth_client, (0, WEEKLY), (7, WEEKLY), (10, 500000), (14, WEEKLY))

    listed = auth_client.get(
        "/api/transactions", params={"account_id": loan, "page_size": 50}
    ).json()["items"]

    by_amount = {t["amount_cents"]: t["repayment"] for t in listed}
    assert by_amount == {WEEKLY: "regular", 500000: "extra"}
    del paid


def test_after_a_repayment_holiday_the_schedule_starts_again() -> None:
    first = [
        models.Transaction(id=f"m{i}", txn_date=dt.date(2026, 1 + i, 1), amount_cents=-543800)
        for i in range(5)
    ]
    resumed = [
        models.Transaction(id=f"n{i}", txn_date=d, amount_cents=-543800)
        for i, d in enumerate(
            [dt.date(2026, 11, 15), dt.date(2026, 12, 15), dt.date(2027, 1, 15),
             dt.date(2027, 2, 15)]
        )
    ]
    assert _extras(first + resumed) == [False] * 9


def test_before_any_pattern_a_second_payment_in_the_same_week_is_extra() -> None:
    assert _extras([_leg(0, WEEKLY), _leg(2, WEEKLY)]) == [False, True]


def test_no_regular_or_extra_choice_when_mortgage_is_gone(auth_client: TestClient) -> None:
    _weeks(auth_client, (0, WEEKLY), (7, WEEKLY))
    mortgage = _cat(auth_client, "Mortgage")
    assert auth_client.delete(f"/api/categories/{mortgage}").status_code == 200

    listed = auth_client.get("/api/transactions", params={"page_size": 50}).json()["items"]

    assert {t["repayment"] for t in listed} == {None}
