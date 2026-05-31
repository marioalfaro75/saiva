from __future__ import annotations

import datetime as dt

from app import models
from app.services import periods
from app.services.merchants import normalise_merchant
from app.services.ml import NaiveBayesCategoriser


def test_merchant_normalisation_known() -> None:
    assert normalise_merchant("WOOLWORTHS 1234 SYDNEY") == "Woolworths"
    assert normalise_merchant("EFTPOS UBER EATS SYDNEY") == "Uber Eats"
    assert normalise_merchant("NETFLIX.COM SYDNEY") == "Netflix"


def test_merchant_normalisation_unknown_is_clean() -> None:
    name = normalise_merchant("POS AUTHORISATION 4321 LOCAL BAKERY")
    assert name
    assert name[0].isupper()


def test_naive_bayes_learns_from_corrections() -> None:
    nb = NaiveBayesCategoriser()
    nb.train(
        [
            ("woolworths metro groceries", "food"),
            ("coles supermarket", "food"),
            ("aldi stores", "food"),
            ("netflix subscription", "stream"),
            ("spotify premium", "stream"),
        ]
    )
    category, confidence = nb.predict("woolworths town hall")
    assert category == "food"
    assert confidence > 0.5


def test_naive_bayes_untrained_returns_none() -> None:
    assert NaiveBayesCategoriser().predict("anything") == (None, 0.0)


def test_fy_bounds() -> None:
    hh = models.Household(name="x", fy_start_month=7, fy_start_day=1)
    assert periods.fy_bounds(hh, dt.date(2025, 8, 15)) == (
        dt.date(2025, 7, 1),
        dt.date(2026, 6, 30),
    )
    assert periods.fy_bounds(hh, dt.date(2025, 3, 15)) == (
        dt.date(2024, 7, 1),
        dt.date(2025, 6, 30),
    )


def test_fortnightly_pay_period() -> None:
    hh = models.Household(
        name="x", period_basis="fortnightly", pay_cycle_anchor=dt.date(2025, 1, 2)
    )
    start, end, _label = periods.current_pay_period(hh, dt.date(2025, 1, 20))
    assert (end - start).days == 13
    assert start <= dt.date(2025, 1, 20) <= end
