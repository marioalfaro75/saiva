"""The Mortgage page (0.19.0).

Asked for: a Mortgage section after Budgets, set up by the household -- the loan and its
offset, what was borrowed and when, the term, the interest rate over time and the
repayments -- with a repayment schedule, a pay-off graph with and without extras, a
what-if, the offset's effect and the next repayment; and net worth from account balances.
"""

from __future__ import annotations

import calendar
import datetime as dt
import itertools

import pytest
from conftest import create_account
from dateutil.relativedelta import relativedelta
from fastapi.testclient import TestClient
from test_transfer_kinds import _add, _scan, _wording

from app.services.mortgage import (
    Rate,
    Scenario,
    State,
    Terms,
    accrued_since_charge,
    due_dates,
    last_charge,
    needed_repayment,
    simulate,
    usual_timing,
)
from app.services.repayments import Period

START = dt.date(2026, 1, 1)
OWING = 50_000_000.0  # $500,000
MONTHLY = 299775  # the textbook repayment for $500,000 at 6% over 30 years


def _terms(percent: float = 6.0, cents: int = MONTHLY, every: str = "monthly") -> Terms:
    return Terms([Rate(None, percent)], [Period(None, cents, every)], START, 1)


# --------------------------------------------------------------------------------------
# The sums
# --------------------------------------------------------------------------------------


def test_a_textbook_loan_is_paid_off_at_the_end_of_its_term() -> None:
    run = simulate(State(START, OWING), _terms(), Scenario())

    assert run.payoff is not None
    assert dt.date(2055, 12, 1) <= run.payoff <= dt.date(2056, 3, 1)
    textbook = MONTHLY * 360 - OWING
    assert abs(run.interest_cents - textbook) / textbook < 0.01


def test_everything_paid_is_what_was_owed_plus_the_interest() -> None:
    run = simulate(State(START, OWING), _terms(), Scenario(extra_cents=12345))

    paid = sum(y.regular_cents + y.extra_cents for y in run.years)
    assert paid == pytest.approx(OWING + run.interest_cents, abs=1)
    assert sum(y.interest_cents for y in run.years) == pytest.approx(run.interest_cents)
    assert run.years[-1].owing_cents == 0


def test_extra_repayments_pay_it_off_sooner_with_less_interest() -> None:
    regular = simulate(State(START, OWING), _terms(), Scenario())
    extra = simulate(State(START, OWING), _terms(), Scenario(extra_cents=50000))

    assert regular.payoff and extra.payoff
    assert extra.payoff < regular.payoff - dt.timedelta(days=365 * 5)
    assert extra.interest_cents < regular.interest_cents


def test_the_offset_saves_interest_and_a_full_offset_saves_all_of_it() -> None:
    regular = simulate(State(START, OWING), _terms(), Scenario())
    offset = simulate(State(START, OWING), _terms(), Scenario(offset_cents=5_000_000))
    full = simulate(State(START, OWING), _terms(), Scenario(offset_cents=OWING))

    assert offset.payoff and regular.payoff and offset.payoff < regular.payoff
    assert offset.interest_cents < regular.interest_cents
    assert full.interest_cents == 0


def test_a_lump_sum_comes_off_at_once() -> None:
    lump = simulate(State(START, OWING), _terms(), Scenario(lump_cents=OWING / 2))
    half = simulate(State(START, OWING / 2), _terms(), Scenario())

    assert lump.payoff == half.payoff
    assert lump.interest_cents == pytest.approx(half.interest_cents)


def test_a_repayment_that_doesnt_cover_the_interest_never_pays_it_off() -> None:
    run = simulate(State(START, OWING), _terms(percent=8.0), Scenario())

    assert run.payoff is None


def test_a_short_first_year_is_not_taken_for_never_paying_it_off() -> None:
    # Interest is charged on the 25th and the first repayment is in January: the loan
    # owes more at the end of its first few days than at the start.
    december = dt.date(2026, 12, 20)
    terms = Terms([Rate(None, 6.0)], [Period(None, MONTHLY, "monthly")], dt.date(2026, 12, 5), 25)

    run = simulate(State(december, OWING), terms, Scenario())

    assert run.payoff is not None


def test_the_rate_follows_its_history() -> None:
    rise = Terms(
        [Rate(None, 6.0), Rate(dt.date(2031, 1, 1), 7.0)],
        [Period(None, MONTHLY * 2, "monthly")],
        START,
        1,
    )
    flat = Terms([Rate(None, 6.0)], [Period(None, MONTHLY * 2, "monthly")], START, 1)

    assert (
        simulate(State(START, OWING), rise, Scenario()).interest_cents
        > simulate(State(START, OWING), flat, Scenario()).interest_cents
    )


def test_a_rate_change_what_if_moves_every_rate() -> None:
    base = simulate(State(START, OWING), _terms(), Scenario())
    lower = simulate(State(START, OWING), _terms(), Scenario(rate_change=-1.0))

    assert lower.payoff and base.payoff and lower.payoff < base.payoff


def test_stopping_part_way_gives_the_state_then() -> None:
    stop = dt.date(2027, 1, 1)
    part = simulate(State(START, OWING), _terms(), Scenario(), stop=stop)

    assert part.state.day == stop
    assert part.state.owing < OWING
    rest = simulate(part.state, _terms(), Scenario())
    whole = simulate(State(START, OWING), _terms(), Scenario())
    assert rest.payoff == whole.payoff


def test_monthly_due_dates_keep_to_the_end_of_the_month() -> None:
    dues = due_dates([Period(None, 1, "monthly")], dt.date(2026, 1, 31), dt.date(2026, 1, 31))

    assert list(itertools.islice(dues, 3)) == [
        dt.date(2026, 2, 28),
        dt.date(2026, 3, 31),
        dt.date(2026, 4, 30),
    ]


def test_a_new_repayment_at_a_new_pace_starts_on_its_date() -> None:
    schedule = [Period(None, 1, "monthly"), Period(dt.date(2026, 3, 5), 1, "fortnightly")]

    dues = due_dates(schedule, dt.date(2026, 1, 15), dt.date(2026, 1, 15))

    assert list(itertools.islice(dues, 4)) == [
        dt.date(2026, 2, 15),
        dt.date(2026, 3, 5),
        dt.date(2026, 3, 19),
        dt.date(2026, 4, 2),
    ]


def test_weekly_repayments_come_every_seven_days() -> None:
    dues = due_dates([Period(None, 1, "weekly")], dt.date(2026, 6, 5), dt.date(2026, 6, 20))

    assert list(itertools.islice(dues, 2)) == [dt.date(2026, 6, 26), dt.date(2026, 7, 3)]


def test_the_needed_repayment_is_the_lenders_sum() -> None:
    assert needed_repayment(OWING, 6.0, "monthly", 360) == MONTHLY
    assert needed_repayment(1200, 0.0, "monthly", 12) == 100
    assert needed_repayment(OWING, 6.0, "monthly", 0) is None


# --------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------

WEEKLY = 70000  # $700 a week


def _pay(client: TestClient, offset: str, loan: str | None, day: dt.date, cents: int) -> None:
    _add(client, offset, day.isoformat(), -cents, "By Authority To Westpac")
    if loan:
        _add(client, loan, day.isoformat(), cents, "REPAYMENT")


def _loan_with_history(client: TestClient, extras: str = "lumps") -> tuple[str, str]:
    """An offset account paying a home loan weekly for 20 weeks, both statements
    imported. Extras: "lumps", $5,000 twice; "monthly", $1,000 on the 10th of each
    month; or "none"."""
    offset = create_account(client, "Offset", "offset")["id"]
    loan = create_account(client, "Home loan", "home_loan")["id"]
    _wording(client, loan, "By Authority To Westpac")
    today = dt.date.today()
    first = today - dt.timedelta(days=7 * 20)
    for week in range(21):
        _pay(client, offset, loan, first + dt.timedelta(days=7 * week), WEEKLY)
        if extras == "lumps" and week in (5, 15):
            _pay(client, offset, loan, first + dt.timedelta(days=7 * week + 3), 500000)
    if extras == "monthly":
        for k in range(0, 7):
            tenth = (today.replace(day=1) - relativedelta(months=k)).replace(day=10)
            if first <= tenth <= today:
                _pay(client, offset, loan, tenth, 100000)
    _scan(client)
    return offset, loan


def _setup(client: TestClient, loan: str, **fields: object) -> dict:
    body: dict = {
        "original_cents": 60_000_000,
        "start_date": "2020-03-15",
        "term_months": 360,
        "rates": [{"since": None, "percent": 6.0}],
        "owing_cents": 45_000_000,
        "repayment_schedule": [{"since": None, "cents": WEEKLY, "every": "weekly"}],
    }
    body.update(fields)
    resp = client.put(f"/api/mortgages/{loan}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _account(client: TestClient, account_id: str) -> dict:
    return client.get(f"/api/accounts/{account_id}").json()


def test_the_page_lists_the_households_home_loans(auth_client: TestClient) -> None:
    assert auth_client.get("/api/mortgages").json() == []
    create_account(auth_client, "Everyday")
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]

    assert auth_client.get("/api/mortgages").json() == [
        {"account_id": loan, "name": "Home loan", "set_up": False}
    ]
    page = auth_client.get(f"/api/mortgages/{loan}").json()
    assert (page["set_up"], page["missing"], page["projection"]) == (False, ["setup"], None)


def test_setting_it_up_makes_the_loan_read_what_is_owed_and_count_in_net_worth(
    auth_client: TestClient,
) -> None:
    offset, loan = _loan_with_history(auth_client)

    page = _setup(
        auth_client, loan, offsets=[{"account_id": offset, "balance_cents": 3_000_000}]
    )

    assert _account(auth_client, loan)["balance_cents"] == -45_000_000
    assert _account(auth_client, offset)["balance_cents"] == 3_000_000
    assert _account(auth_client, loan)["in_net_worth"] is True
    assert page["offsets"] == [
        {"account_id": offset, "name": "Offset", "balance_cents": 3_000_000}
    ]
    worth = auth_client.get("/api/net-worth").json()
    assert (worth["assets_cents"], worth["liabilities_cents"]) == (3_000_000, 45_000_000)
    assert worth["history"][-1]["net_cents"] == -42_000_000
    assert auth_client.get("/api/mortgages").json()[0]["set_up"] is True


def test_the_projection_as_you_pay_without_extras_and_without_the_offset(
    auth_client: TestClient,
) -> None:
    offset, loan = _loan_with_history(auth_client, extras="monthly")

    p = _setup(
        auth_client, loan, offsets=[{"account_id": offset, "balance_cents": 3_000_000}]
    )["projection"]

    assert p["owing_cents"] == 45_000_000
    assert p["estimated"] is False
    assert p["offset_cents"] == 3_000_000
    assert p["usual_extra_cents"] == 100000  # $1,000 most months
    assert p["as_you_pay"]["payoff"] < p["regular_only"]["payoff"]
    assert p["as_you_pay"]["interest_cents"] < p["regular_only"]["interest_cents"]
    assert p["as_you_pay"]["payoff"] < p["without_offset"]["payoff"]
    assert p["without_offset"]["interest_cents"] > p["as_you_pay"]["interest_cents"]
    assert p["what_if"] is None
    assert p["next_due"] == (dt.date.today() + dt.timedelta(days=7)).isoformat()
    assert (p["next_cents"], p["every"], p["rate"]) == (WEEKLY, "weekly", 6.0)
    assert p["contract_end"] == "2050-03-15"
    assert p["points"][0]["as_you_pay"] < 45_000_000
    assert p["points"][-1]["regular_only"] == 0
    assert p["schedule"][0]["year"] == dt.date.today().year
    assert p["schedule"][-1]["owing_cents"] == 0


def test_the_months_paid_show_regular_and_extra_repayments_apart(
    auth_client: TestClient,
) -> None:
    _, loan = _loan_with_history(auth_client)

    paid = _setup(auth_client, loan)["paid"]

    assert len(paid) == 12
    assert paid[0]["month"] == dt.date.today().strftime("%Y-%m")
    assert sum(m["extra_cents"] for m in paid) == 1_000_000
    assert sum(m["regular_cents"] for m in paid) == 21 * WEEKLY


def test_a_what_if_is_tried_without_saving_anything(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client)
    _setup(auth_client, loan)

    def what_if(**params: object) -> dict:
        resp = auth_client.get(f"/api/mortgages/{loan}", params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()["projection"]

    base = what_if()
    more = what_if(extra_cents=20000)
    rise = what_if(rate_change=1.0)
    lump = what_if(lump_cents=10_000_000)
    offset = what_if(offset_change_cents=5_000_000)

    assert more["what_if"]["payoff"] < base["as_you_pay"]["payoff"]
    assert rise["what_if"]["payoff"] > base["as_you_pay"]["payoff"]
    assert rise["what_if_needed_cents"] > rise["needed_cents"]
    assert lump["what_if"]["interest_cents"] < base["as_you_pay"]["interest_cents"]
    assert offset["what_if"]["interest_cents"] < base["as_you_pay"]["interest_cents"]
    assert all("what_if" in row for row in more["points"])
    assert more["what_if_schedule"]
    assert what_if()["what_if"] is None
    assert _account(auth_client, loan)["balance_cents"] == -45_000_000


def test_without_a_rate_there_is_no_projection_yet(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client)

    page = _setup(auth_client, loan, rates=[])

    assert page["missing"] == ["rate"]
    assert page["projection"] is None


def test_without_a_repayment_saiva_suggests_one(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client, extras="none")

    page = _setup(auth_client, loan, repayment_schedule=[])

    assert page["missing"] == ["repayment"]
    assert page["repayment_suggestion"]["cents"] == WEEKLY
    assert page["repayment_suggestion"]["every"] == "weekly"


def test_owing_on_a_past_date_is_carried_to_today_as_an_estimate(
    auth_client: TestClient,
) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    months_ago = dt.date.today() - dt.timedelta(days=90)

    p = _setup(auth_client, loan, owing_on=months_ago.isoformat())["projection"]

    assert p["estimated"] is True
    assert p["known_on"] == months_ago.isoformat()
    assert p["known_cents"] == 45_000_000
    # Thirteen weeks of $700 less about three months' interest at 6% on $450,000.
    assert 45_000_000 - 13 * WEEKLY < p["owing_cents"] < 45_000_000


def test_a_loan_statement_after_the_date_keeps_what_is_owed_right(
    auth_client: TestClient,
) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    month_ago = dt.date.today() - dt.timedelta(days=30)
    _setup(auth_client, loan, owing_on=month_ago.isoformat())
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    _add(auth_client, loan, week_ago, -200000, "INTEREST")
    _add(auth_client, loan, week_ago, 1_000_000, "REPAYMENT")

    p = auth_client.get(f"/api/mortgages/{loan}").json()["projection"]

    assert p["known_on"] == week_ago
    assert p["known_cents"] == 45_000_000 + 200000 - 1_000_000


def test_leaving_what_is_owed_out_keeps_it(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client)
    _setup(auth_client, loan)

    _setup(auth_client, loan, owing_cents=None, rates=[{"since": None, "percent": 5.5}])

    assert _account(auth_client, loan)["balance_cents"] == -45_000_000


def test_a_rate_history_is_kept_oldest_first_one_a_date(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client)

    page = _setup(
        auth_client,
        loan,
        rates=[
            {"since": "2025-06-01", "percent": 6.5},
            {"since": None, "percent": 5.9},
            {"since": "2025-06-01", "percent": 6.24},
        ],
    )

    assert page["rates"] == [
        {"since": None, "percent": 5.9},
        {"since": "2025-06-01", "percent": 6.24},
    ]
    assert page["projection"]["rate"] == 6.24


@pytest.mark.parametrize(
    "fields",
    [
        {"owing_on": "2100-01-01"},  # in the future
        {"rates": [{"since": None, "percent": 31}]},
        {"term_months": 0},
        {"owing_cents": -1},
    ],
)
def test_impossible_setups_are_refused(auth_client: TestClient, fields: dict) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    body = {"rates": [{"since": None, "percent": 6.0}], **fields}

    assert auth_client.put(f"/api/mortgages/{loan}", json=body).status_code == 422


def test_an_offset_must_be_an_account_that_holds_money(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    card = create_account(auth_client, "Card", "credit_card")["id"]

    for account in (card, loan, "nope"):
        resp = auth_client.put(
            f"/api/mortgages/{loan}", json={"offsets": [{"account_id": account}]}
        )
        assert resp.status_code == 422, account


def test_only_a_home_loan_has_a_mortgage_page(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday")["id"]

    assert auth_client.get(f"/api/mortgages/{everyday}").status_code == 422
    assert auth_client.get("/api/mortgages/nope").status_code == 404


def test_forgetting_the_setup_keeps_the_accounts(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client)
    _setup(auth_client, loan)

    assert auth_client.delete(f"/api/mortgages/{loan}").status_code == 204

    assert auth_client.get(f"/api/mortgages/{loan}").json()["set_up"] is False
    assert _account(auth_client, loan)["balance_cents"] == -45_000_000


# --------------------------------------------------------------------------------------
# Net worth from accounts
# --------------------------------------------------------------------------------------


def test_an_account_counts_in_net_worth_once_the_household_says(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, savings, dt.date.today().isoformat(), 250000, "Pay")

    before = auth_client.get("/api/net-worth").json()
    assert before["net_cents"] == 0
    assert before["accounts"] == [
        {
            "id": savings,
            "name": "Savings",
            "type": "savings",
            "balance_cents": 250000,
            "in_net_worth": False,
        }
    ]

    resp = auth_client.patch(f"/api/accounts/{savings}", json={"in_net_worth": True})
    assert resp.status_code == 200, resp.text

    after = auth_client.get("/api/net-worth").json()
    assert (after["assets_cents"], after["net_cents"]) == (250000, 250000)
    assert after["history"][-1]["net_cents"] == 250000


def test_an_accounts_balance_can_be_set_as_of_a_date(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    today = dt.date.today()
    _add(auth_client, savings, (today - dt.timedelta(days=10)).isoformat(), 10000, "Pay")
    _add(auth_client, savings, (today - dt.timedelta(days=2)).isoformat(), 5000, "Pay")

    resp = auth_client.patch(
        f"/api/accounts/{savings}",
        json={"balance_cents": 100000, "balance_on": (today - dt.timedelta(days=5)).isoformat()},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["opening_balance_cents"] == 90000
    assert resp.json()["balance_cents"] == 105000
    future = (today + dt.timedelta(days=2)).isoformat()  # tomorrow is today in Sydney
    refused = auth_client.patch(
        f"/api/accounts/{savings}", json={"balance_cents": 1, "balance_on": future}
    )
    assert refused.status_code == 422
    assert _account(auth_client, savings)["balance_cents"] == 105000


def test_a_null_in_net_worth_leaves_it(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    auth_client.patch(f"/api/accounts/{savings}", json={"in_net_worth": True})

    resp = auth_client.patch(f"/api/accounts/{savings}", json={"in_net_worth": None})

    assert resp.status_code == 200
    assert resp.json()["in_net_worth"] is True


# --------------------------------------------------------------------------------------
# What the review found (0.19.0)
# --------------------------------------------------------------------------------------


def test_a_one_off_lump_sum_is_not_a_usual_extra(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client, extras="lumps")

    p = _setup(auth_client, loan)["projection"]

    assert p["usual_extra_cents"] == 0
    assert p["as_you_pay"] == p["regular_only"]
    # What the two lumps did: without them it would be paid off later, with more interest.
    assert p["extras_made_cents"] == 1_000_000
    without = p["without_extras_made"]
    assert without["payoff"] > p["regular_only"]["payoff"]
    assert without["interest_cents"] > p["regular_only"]["interest_cents"]


def test_without_extras_made_there_is_nothing_to_compare(auth_client: TestClient) -> None:
    _, loan = _loan_with_history(auth_client, extras="none")

    p = _setup(auth_client, loan)["projection"]

    assert (p["extras_made_cents"], p["without_extras_made"]) == (0, None)


def test_repayments_made_since_the_loan_statement_come_off_what_is_owed(
    auth_client: TestClient,
) -> None:
    # The loan's statement is imported to four weeks ago; the offset's to today.
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    today = dt.date.today()
    for week in range(9):
        day = today - dt.timedelta(days=7 * (8 - week))
        _pay(auth_client, offset, loan if week <= 4 else None, day, WEEKLY)
    _scan(auth_client)
    four_weeks_ago = today - dt.timedelta(days=28)
    _setup(auth_client, loan, owing_on=four_weeks_ago.isoformat())

    p = auth_client.get(f"/api/mortgages/{loan}").json()["projection"]

    assert p["known_on"] == four_weeks_ago.isoformat()
    assert p["estimated"] is True
    # Four repayments since, less about four weeks' interest at 6% on $450,000.
    interest = 45_000_000 * 0.06 * 28 / 365
    assert p["owing_cents"] == pytest.approx(45_000_000 - 4 * WEEKLY + interest, abs=100_000)
    assert p["next_due"] == (today + dt.timedelta(days=7)).isoformat()


def test_a_repayment_the_loan_shows_a_day_later_is_not_lost(auth_client: TestClient) -> None:
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    _pay(auth_client, offset, loan, week_ago, WEEKLY)
    _setup(auth_client, loan, owing_on=week_ago.isoformat())
    _add(auth_client, offset, today.isoformat(), -WEEKLY, "By Authority To Westpac")
    _add(auth_client, loan, (today + dt.timedelta(days=1)).isoformat(), WEEKLY, "REPAYMENT")
    _scan(auth_client)

    p = auth_client.get(f"/api/mortgages/{loan}").json()["projection"]

    # Interest is charged on the 15th (the loan started on the 15th); today's $700 is off.
    charge = last_charge(today, 15)
    days = (charge - last_charge(week_ago, 15)).days if charge > week_ago else 0
    charged = 45_000_000 * 0.06 * days / 365
    assert p["owing_cents"] == pytest.approx(45_000_000 + charged - WEEKLY, abs=2)


def test_what_was_said_is_owed_stays_when_older_statements_come_later(
    auth_client: TestClient,
) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _setup(auth_client, loan)
    today = dt.date.today()
    for months in range(1, 7):
        day = (today - relativedelta(months=months)).isoformat()
        _add(auth_client, loan, day, 299775, "REPAYMENT")
        _add(auth_client, loan, day, -225000, "INTEREST")

    assert _account(auth_client, loan)["balance_cents"] == -45_000_000
    page = auth_client.get(f"/api/mortgages/{loan}").json()
    assert page["projection"]["known_cents"] == 45_000_000
    assert page["balance_as_of"] == today.isoformat()

    # A later row does move it: it's after what was said.
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    _add(auth_client, loan, tomorrow, 100000, "REPAYMENT")
    assert _account(auth_client, loan)["balance_cents"] == -44_900_000


def test_deleting_a_transaction_keeps_what_was_said_too(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    old = _add(auth_client, savings, "2026-01-05", 5000, "Pay")
    auth_client.patch(f"/api/accounts/{savings}", json={"balance_cents": 100000})

    assert auth_client.delete(f"/api/transactions/{old}").status_code == 204

    assert _account(auth_client, savings)["balance_cents"] == 100000


def test_setting_the_opening_balance_drops_what_was_said(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    auth_client.patch(f"/api/accounts/{savings}", json={"balance_cents": 100000})
    auth_client.patch(f"/api/accounts/{savings}", json={"opening_balance_cents": 500})

    _add(auth_client, savings, "2026-01-05", 5000, "Pay")

    assert _account(auth_client, savings)["balance_cents"] == 5500


def test_the_interest_since_the_last_charge_is_owed_too() -> None:
    rates = [Rate(None, 6.0)]
    # Charged on the 15th; the balance is from the 14th: a month of interest is due.
    assert last_charge(dt.date(2026, 11, 14), 15) == dt.date(2026, 10, 15)
    assert last_charge(dt.date(2026, 11, 15), 15) == dt.date(2026, 11, 15)
    assert last_charge(dt.date(2026, 3, 1), 31) == dt.date(2026, 2, 28)
    month = accrued_since_charge(dt.date(2026, 11, 14), OWING, 0, rates, 15)
    assert month == pytest.approx(OWING * 0.06 * 30 / 365)
    assert accrued_since_charge(dt.date(2026, 11, 15), OWING, 0, rates, 15) == 0
    assert accrued_since_charge(dt.date(2026, 11, 14), OWING, OWING, rates, 15) == 0


def test_a_monthly_repayment_on_the_31st_comes_back_to_the_31st() -> None:
    days = [dt.date(2026, 10, 31), dt.date(2026, 12, 31), dt.date(2027, 1, 31),
            dt.date(2027, 2, 28)]
    timing = usual_timing(days, "monthly")
    assert timing == (dt.date(2027, 2, 28), 31)

    dues = due_dates([Period(None, 1, "monthly")], timing[0], dt.date(2027, 3, 1), timing[1])

    assert list(itertools.islice(dues, 2)) == [dt.date(2027, 3, 31), dt.date(2027, 4, 30)]


def test_a_monthly_repayment_made_early_is_next_months() -> None:
    days = [dt.date(2026, 9, 1), dt.date(2026, 10, 1), dt.date(2026, 10, 30)]

    assert usual_timing(days, "monthly") == (dt.date(2026, 11, 1), 1)


def test_a_weekly_repayment_held_over_the_weekend_is_still_due_on_friday() -> None:
    fridays = [dt.date(2026, 10, 2), dt.date(2026, 10, 9), dt.date(2026, 10, 16)]
    tuesday = dt.date(2026, 10, 27)  # the 23rd held over

    anchor, _ = usual_timing([*fridays, tuesday], "weekly") or (None, None)

    assert anchor == dt.date(2026, 10, 23)


def test_a_year_starting_on_new_years_eve_is_labelled_by_its_year() -> None:
    eve = dt.date(2026, 12, 31)

    run = simulate(State(eve, OWING), _terms(), Scenario())

    assert [y.year for y in run.years[:2]] == [2027, 2028]


def test_a_lump_sum_shows_in_the_first_years_extras() -> None:
    run = simulate(State(START, OWING), _terms(), Scenario(lump_cents=1_000_000))

    assert run.years[0].extra_cents == 1_000_000
    paid = sum(y.regular_cents + y.extra_cents for y in run.years)
    assert paid == pytest.approx(OWING + run.interest_cents, abs=1)


def test_never_paid_off_says_why() -> None:
    stalls = simulate(State(START, OWING), _terms(percent=8.0), Scenario())
    # Just above the interest: owing falls every year, but not within 50 years.
    slow = simulate(State(START, OWING), _terms(cents=251000), Scenario())

    assert (stalls.payoff, stalls.stalls) == (None, True)
    assert (slow.payoff, slow.stalls) == (None, False)


def test_a_line_that_never_pays_off_is_not_drawn_down_to_nothing() -> None:
    from app.services.mortgage import _points

    paid = simulate(State(START, OWING), _terms(), Scenario())
    never = simulate(State(START, OWING), _terms(percent=8.0), Scenario())

    rows = _points({"as_you_pay": paid, "what_if": never})

    assert all(row.get("what_if", 1) != 0 for row in rows)
    assert rows[-1]["as_you_pay"] == 0


def test_usual_extras_follow_a_change_of_how_often() -> None:
    schedule = [Period(None, MONTHLY, "monthly"), Period(dt.date(2026, 7, 1), 70000, "weekly")]
    terms = Terms([Rate(None, 6.0)], schedule, START, 1)

    run = simulate(
        State(START, OWING), terms, Scenario(extra_monthly=52000), stop=dt.date(2027, 1, 1)
    )

    # $520 a month: $520 a monthly repayment (five, February to June), then $120 a
    # weekly one.
    year = run.years[0]
    weeks = round((year.regular_cents - 5 * MONTHLY) / 70000)
    assert weeks == 27
    assert year.extra_cents == pytest.approx(5 * 52000 + weeks * 12000)


def test_a_set_up_loan_can_be_deleted_and_stops_offsetting(auth_client: TestClient) -> None:
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    other = create_account(auth_client, "Other loan", "home_loan")["id"]
    _setup(auth_client, loan, offsets=[{"account_id": offset}])
    _setup(auth_client, other, offsets=[{"account_id": offset}])

    assert auth_client.delete(f"/api/accounts/{loan}").status_code == 204
    assert auth_client.delete(f"/api/accounts/{offset}").status_code == 204

    assert [m["account_id"] for m in auth_client.get("/api/mortgages").json()] == [other]
    assert auth_client.get(f"/api/mortgages/{other}").json()["offsets"] == []


def test_a_far_off_start_date_is_refused_not_saved(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]

    resp = auth_client.put(f"/api/mortgages/{loan}", json={"start_date": "9990-01-01"})

    assert resp.status_code == 422
    assert auth_client.get(f"/api/mortgages/{loan}").json()["set_up"] is False


def test_tomorrow_is_today_somewhere(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()

    page = _setup(auth_client, loan, owing_on=tomorrow)

    assert page["balance_as_of"] == tomorrow
    assert page["projection"]["owing_cents"] == 45_000_000


def test_the_page_names_a_mortgage_entered_by_hand_on_net_worth(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    auth_client.post(
        "/api/net-worth/items", json={"name": "Mortgage", "kind": "liability", "value_cents": 1}
    )

    assert _setup(auth_client, loan)["manual_liabilities"] == ["Mortgage"]


def test_net_worth_today_is_todays_figures(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    auth_client.patch(f"/api/accounts/{savings}", json={"in_net_worth": True})
    _add(auth_client, savings, dt.date.today().isoformat(), 250000, "Pay")  # no snapshot

    worth = auth_client.get("/api/net-worth").json()

    assert worth["net_cents"] == 250000
    assert worth["history"][-1] == {
        "as_of": dt.date.today().isoformat(),
        "assets_cents": 250000,
        "liabilities_cents": 0,
        "net_cents": 250000,
    }


def test_taking_the_last_account_out_of_net_worth_is_recorded(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, savings, dt.date.today().isoformat(), 250000, "Pay")
    auth_client.patch(f"/api/accounts/{savings}", json={"in_net_worth": True})

    auth_client.patch(f"/api/accounts/{savings}", json={"in_net_worth": False})

    from app.db import SessionLocal
    from app.models import NetWorthSnapshot

    with SessionLocal() as db:
        snap = db.query(NetWorthSnapshot).one()
        assert snap.net_cents == 0


# --------------------------------------------------------------------------------------
# What the second review found (0.19.0)
# --------------------------------------------------------------------------------------


def test_importing_one_accounts_statement_keeps_its_stated_balance(
    auth_client: TestClient,
) -> None:
    import json
    from pathlib import Path

    savings = create_account(auth_client, "Savings", "savings")["id"]
    auth_client.patch(f"/api/accounts/{savings}", json={"balance_cents": 100000})
    csv = (Path(__file__).parent / "fixtures" / "cba_sample.csv").read_bytes()
    mapping = auth_client.post(
        "/api/imports/sniff", files={"file": ("cba.csv", csv, "text/csv")}
    ).json()["suggested_mapping"]

    commit = auth_client.post(
        "/api/imports/commit",
        files={"file": ("cba.csv", csv, "text/csv")},
        data={"account_id": savings, "file_format": "csv", "mapping": json.dumps(mapping)},
    )

    assert commit.json()["added"] == 6
    assert _account(auth_client, savings)["balance_cents"] == 100000


def test_a_transaction_typed_in_after_the_balance_was_given_counts(
    auth_client: TestClient,
) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    today = dt.date.today().isoformat()
    _add(auth_client, savings, today, -2000, "Coffee")  # before: in the figure
    auth_client.patch(f"/api/accounts/{savings}", json={"balance_cents": 100000})

    _add(auth_client, savings, today, -500, "Lunch")  # after: new
    old = _add(auth_client, savings, "2026-01-05", 7000, "Pay")  # earlier: in the figure

    assert _account(auth_client, savings)["balance_cents"] == 99500
    auth_client.delete(f"/api/transactions/{old}")
    assert _account(auth_client, savings)["balance_cents"] == 99500


def test_a_balance_too_large_to_hold_is_refused_not_a_crash(auth_client: TestClient) -> None:
    savings = create_account(auth_client, "Savings", "savings")["id"]
    _add(auth_client, savings, "2026-01-05", -1_000_000_000, "Big")

    resp = auth_client.patch(f"/api/accounts/{savings}", json={"balance_cents": 2_000_000_000})

    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"]


def test_offsets_are_set_as_of_the_households_today(auth_client: TestClient) -> None:
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()  # Sydney's today
    _add(auth_client, offset, tomorrow, -5000, "Coffee this morning")

    _setup(
        auth_client,
        loan,
        offsets=[{"account_id": offset, "balance_cents": 3_000_000}],
        today=tomorrow,
    )

    assert _account(auth_client, offset)["balance_cents"] == 3_000_000


def test_only_a_liability_that_looks_like_a_home_loan_is_named(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    for name in ("Car loan", "HECS", "House loan"):
        auth_client.post(
            "/api/net-worth/items", json={"name": name, "kind": "liability", "value_cents": 1}
        )

    assert _setup(auth_client, loan)["manual_liabilities"] == ["House loan"]


def test_net_worth_too_large_to_snapshot_still_saves(auth_client: TestClient) -> None:
    for name in ("A", "B"):
        account = create_account(auth_client, name, "savings")["id"]
        resp = auth_client.patch(
            f"/api/accounts/{account}", json={"balance_cents": 1_500_000_000, "in_net_worth": True}
        )
        assert resp.status_code == 200, resp.text

    assert auth_client.get("/api/net-worth").json()["net_cents"] == 3_000_000_000


def test_one_lump_in_a_short_history_is_not_usual(auth_client: TestClient) -> None:
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    today = dt.date.today()
    two_months = today.replace(day=1) - relativedelta(months=2)
    for week in range(12):
        day = two_months + dt.timedelta(days=7 * week)
        if day <= today:
            _pay(auth_client, offset, loan, day, WEEKLY)
    _pay(auth_client, offset, loan, two_months + dt.timedelta(days=10), 500000)
    _scan(auth_client)

    p = _setup(auth_client, loan)["projection"]

    assert p["usual_extra_cents"] == 0


def test_the_lenders_charge_day_comes_from_the_statement(auth_client: TestClient) -> None:
    from app.db import SessionLocal
    from app.models import Account
    from app.services.mortgage import charge_day_of

    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    for day in ("2026-08-31", "2026-09-30", "2026-10-31"):
        _add(auth_client, loan, day, -225000, "INTEREST")

    with SessionLocal() as db:
        account = db.get(Account, loan)
        assert account is not None
        assert charge_day_of(db, account) == 31
    # The statement ends on a charge day: nothing has built up since.
    assert accrued_since_charge(dt.date(2026, 10, 31), OWING, 0, [Rate(None, 6.0)], 31) == 0


def test_a_tie_on_the_payment_day_goes_to_the_new_day() -> None:
    days = [dt.date(2026, m, 15) for m in (5, 6, 7)] + [dt.date(2026, m, 1) for m in (8, 9, 10)]

    assert usual_timing(days, "monthly") == (dt.date(2026, 10, 1), 1)


def test_a_rate_cut_still_to_come_is_not_never() -> None:
    terms = Terms(
        [Rate(None, 8.0), Rate(dt.date(2028, 1, 1), 5.0)],
        [Period(None, MONTHLY, "monthly")],
        START,
        1,
    )

    run = simulate(State(START, OWING), terms, Scenario())

    assert run.payoff is not None and not run.stalls


def test_a_lump_that_clears_everything_pays_the_interest_built_up_too() -> None:
    start = State(START, OWING, accrued=12345.0)

    run = simulate(start, _terms(), Scenario(lump_cents=OWING + 12345))

    assert run.payoff == START
    assert run.interest_cents == pytest.approx(12345)
    assert run.years[0].extra_cents == pytest.approx(OWING + 12345)


def test_after_a_change_of_pace_the_first_new_repayment_counts_once(
    auth_client: TestClient,
) -> None:
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    today = dt.date.today()
    base = today - dt.timedelta(days=10)
    for k in range(6):
        _pay(auth_client, offset, loan, base - dt.timedelta(days=7 * k), WEEKLY)
    changed = base + dt.timedelta(days=4)
    _pay(auth_client, offset, None, changed, 2 * WEEKLY)  # the loan's statement isn't in yet
    _scan(auth_client)
    p = _setup(
        auth_client,
        loan,
        owing_on=base.isoformat(),
        repayment_schedule=[
            {"since": None, "cents": WEEKLY, "every": "weekly"},
            {"since": changed.isoformat(), "cents": 2 * WEEKLY, "every": "fortnightly"},
        ],
    )["projection"]

    # $1,400 off once, and the interest charged on the 15th in between, if any.
    charge = last_charge(today, 15)
    days = (charge - last_charge(base, 15)).days if charge > base else 0
    charged = 45_000_000 * 0.06 * days / 365
    assert p["owing_cents"] == pytest.approx(45_000_000 + charged - 2 * WEEKLY, abs=1500)
    assert p["next_due"] == (changed + dt.timedelta(days=14)).isoformat()


def test_a_change_to_monthly_takes_its_day_from_the_monthly_repayment(
    auth_client: TestClient,
) -> None:
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    today = dt.date.today()
    monthly_from = today - dt.timedelta(days=5)
    for k in range(1, 7):
        _pay(auth_client, offset, loan, monthly_from - dt.timedelta(days=7 * k - 2), WEEKLY)
    _pay(auth_client, offset, loan, monthly_from, 300000)
    _scan(auth_client)

    p = _setup(
        auth_client,
        loan,
        repayment_schedule=[
            {"since": None, "cents": WEEKLY, "every": "weekly"},
            {"since": monthly_from.isoformat(), "cents": 300000, "every": "monthly"},
        ],
    )["projection"]

    assert p["next_due"] == (monthly_from + relativedelta(months=1)).isoformat()


def test_usual_extras_are_not_assumed_on_top_of_the_ones_seen_to_today(
    auth_client: TestClient,
) -> None:
    # No interest (0%), so what is owed is plain sums. The regular repayment goes from an
    # account imported only to five weeks ago; the extras from the offset, to today.
    offset = create_account(auth_client, "Offset", "offset")["id"]
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    _wording(auth_client, loan, "By Authority To Westpac")
    today = dt.date.today()
    last_regular = today - dt.timedelta(days=35)
    for k in range(30):
        _pay(auth_client, offset, None, last_regular - dt.timedelta(days=7 * k), WEEKLY)
    tenths = [
        (today.replace(day=1) - relativedelta(months=k)).replace(day=10) for k in range(8)
    ]
    for day in tenths:
        if day <= today:
            _pay(auth_client, offset, None, day, 100000)
    known_on = today - dt.timedelta(days=30)

    p = _setup(
        auth_client, loan, owing_on=known_on.isoformat(), rates=[{"since": None, "percent": 0}]
    )["projection"]

    assert p["usual_extra_cents"] == 100000
    dues = [last_regular + dt.timedelta(days=7 * k) for k in range(1, 10)]
    scheduled = sum(1 for d in dues if known_on < d <= today)
    extras = sum(1 for d in tenths if known_on < d <= today)
    assert p["owing_cents"] == 45_000_000 - scheduled * WEEKLY - extras * 100000


def test_the_projection_charges_interest_on_the_statements_day(auth_client: TestClient) -> None:
    loan = create_account(auth_client, "Home loan", "home_loan")["id"]
    today = dt.date.today()
    month_end = today.replace(day=1) - dt.timedelta(days=1)
    if today.day == calendar.monthrange(today.year, today.month)[1]:
        pytest.skip("on a month's last day the next charge is today")
    for k in range(3):
        day = (month_end.replace(day=1) - relativedelta(months=k))
        day = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        _add(auth_client, loan, day.isoformat(), -225000, "INTEREST")
    # The loan started on the 1st, but the lender charges at the end of each month.
    p = _setup(
        auth_client,
        loan,
        owing_on=(month_end - relativedelta(months=3)).isoformat(),
        start_date="2020-03-01",
        repayment_schedule=[
            {"since": month_end.isoformat(), "cents": 300000, "every": "monthly"}
        ],
    )["projection"]

    assert p["known_on"] == month_end.isoformat()
    assert p["owing_cents"] == p["known_cents"]  # nothing charged since the month's end


def test_a_net_worth_too_large_for_a_snapshot_is_not_written(auth_client: TestClient) -> None:
    from app.db import SessionLocal
    from app.models import User
    from app.services.networth import record_snapshot

    for name in ("A", "B"):
        account = create_account(auth_client, name, "savings")["id"]
        auth_client.patch(
            f"/api/accounts/{account}", json={"balance_cents": 1_500_000_000, "in_net_worth": True}
        )

    with SessionLocal() as db:
        household = db.query(User).first().household_id  # type: ignore[union-attr]
        assert record_snapshot(db, household) is None
