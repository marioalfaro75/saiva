"""The Mortgage page (0.19.0).

Asked for: a Mortgage section after Budgets, set up by the household -- the loan and its
offset, what was borrowed and when, the term, the interest rate over time and the
repayments -- with a repayment schedule, a pay-off graph with and without extras, a
what-if, the offset's effect and the next repayment; and net worth from account balances.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from conftest import create_account
from fastapi.testclient import TestClient
from test_transfer_kinds import _add, _scan, _wording

from app.services.mortgage import (
    Rate,
    Scenario,
    State,
    Terms,
    due_dates,
    needed_repayment,
    simulate,
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


def _loan_with_history(client: TestClient, extras: bool = True) -> tuple[str, str]:
    """An offset account paying a home loan weekly for 20 weeks, with an extra $5,000
    twice, and both statements imported."""
    offset = create_account(client, "Offset", "offset")["id"]
    loan = create_account(client, "Home loan", "home_loan")["id"]
    _wording(client, loan, "By Authority To Westpac")
    today = dt.date.today()
    first = today - dt.timedelta(days=7 * 20)
    for week in range(21):
        date = (first + dt.timedelta(days=7 * week)).isoformat()
        _add(client, offset, date, -WEEKLY, "By Authority To Westpac")
        _add(client, loan, date, WEEKLY, "REPAYMENT")
        if extras and week in (5, 15):
            later = (first + dt.timedelta(days=7 * week + 3)).isoformat()
            _add(client, offset, later, -500000, "By Authority To Westpac")
            _add(client, loan, later, 500000, "REPAYMENT")
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
    offset, loan = _loan_with_history(auth_client)

    p = _setup(
        auth_client, loan, offsets=[{"account_id": offset, "balance_cents": 3_000_000}]
    )["projection"]

    assert p["owing_cents"] == 45_000_000
    assert p["estimated"] is False
    assert p["offset_cents"] == 3_000_000
    # $10,000 of extras over the five months there are repayments for.
    assert p["usual_extra_cents"] == pytest.approx(1_000_000 / 5, rel=0.25)
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
    _, loan = _loan_with_history(auth_client, extras=False)

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
    future = (today + dt.timedelta(days=1)).isoformat()
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
