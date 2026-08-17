"""Server-side sorting and per-column filtering of the transactions list.

This list is paginated, so ordering and filtering have to happen in the database.
Doing either in the browser would silently operate on the fetched page alone —
looking exactly like a real sort while answering a different question.
"""

from __future__ import annotations

from conftest import create_account
from fastapi.testclient import TestClient


def _seed(client: TestClient) -> dict[str, str]:
    everyday = create_account(client, "Everyday", "everyday")
    savings = create_account(client, "Savings", "savings")
    categories = {c["name"]: c["id"] for c in client.get("/api/categories").json()}
    rows = [
        # date,        account,   amount,  description,            category
        ("2025-06-01", everyday, -8540, "WOOLWORTHS METRO", "Supermarkets"),
        ("2025-06-05", savings, 120, "INTEREST PAID", None),
        ("2025-06-03", everyday, -100000, "RENT PAYMENT", "Rent"),
        ("2025-06-02", savings, -900, "ATM WITHDRAWAL", None),
    ]
    for date, account, amount, description, category in rows:
        body = {
            "account_id": account["id"],
            "txn_date": date,
            "amount_cents": amount,
            "description": description,
        }
        if category and category in categories:
            body["category_id"] = categories[category]
        resp = client.post("/api/transactions", json=body)
        assert resp.status_code in (200, 201), resp.text
    return {"everyday": everyday["id"], "savings": savings["id"]}


def _descriptions(payload: dict) -> list[str]:
    return [t["raw_description"] for t in payload["items"]]


def _get(client: TestClient, **params: object) -> dict:
    resp = client.get("/api/transactions", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------------- sorting


def test_sort_by_amount_orders_numerically(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert [t["amount_cents"] for t in _get(auth_client, sort="amount", dir="asc")["items"]] == [
        -100000,
        -8540,
        -900,
        120,
    ]
    assert [t["amount_cents"] for t in _get(auth_client, sort="amount", dir="desc")["items"]] == [
        120,
        -900,
        -8540,
        -100000,
    ]


def test_sort_by_date(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert [t["txn_date"] for t in _get(auth_client, sort="date", dir="asc")["items"]] == [
        "2025-06-01",
        "2025-06-02",
        "2025-06-03",
        "2025-06-05",
    ]


def test_sort_by_account_and_category_uses_the_name(auth_client: TestClient) -> None:
    _seed(auth_client)
    accounts = [t["account_name"] for t in _get(auth_client, sort="account", dir="asc")["items"]]
    assert accounts == ["Everyday", "Everyday", "Savings", "Savings"]

    names = [t["category_name"] for t in _get(auth_client, sort="category", dir="asc")["items"]]
    assert names[:2] == ["Rent", "Supermarkets"]


def test_uncategorised_rows_sort_last_in_both_directions(auth_client: TestClient) -> None:
    """A row with no category must never top the list just because it is empty."""
    account = create_account(auth_client)
    for description in ("WOOLWORTHS METRO", "ZZQX UNRECOGNISED PAYEE"):
        auth_client.post(
            "/api/transactions",
            json={
                "account_id": account["id"],
                "txn_date": "2025-06-01",
                "amount_cents": -1000,
                "description": description,
            },
        )
    for direction in ("asc", "desc"):
        names = [
            t["category_name"] for t in _get(auth_client, sort="category", dir=direction)["items"]
        ]
        assert None in names, "expected an uncategorised row to exercise the ordering"
        # Once a blank appears, everything after it must also be blank.
        first_blank = names.index(None)
        assert all(n is None for n in names[first_blank:]), f"{direction}: {names}"


def test_left_join_keeps_rows_that_have_no_category(auth_client: TestClient) -> None:
    """Sorting by a joined column must not quietly drop rows missing that relation."""
    _seed(auth_client)
    for sort in ("category", "account"):
        assert _get(auth_client, sort=sort)["total"] == 4, sort
        assert len(_get(auth_client, sort=sort)["items"]) == 4, sort


def test_sort_is_applied_before_pagination(auth_client: TestClient) -> None:
    """The regression that matters: page 1 of amount-descending must hold the largest
    amounts overall, not the largest of whichever rows happen to be fetched first."""
    _seed(auth_client)
    first = _get(auth_client, sort="amount", dir="desc", page=1, page_size=2)
    second = _get(auth_client, sort="amount", dir="desc", page=2, page_size=2)
    assert [t["amount_cents"] for t in first["items"]] == [120, -900]
    assert [t["amount_cents"] for t in second["items"]] == [-8540, -100000]
    assert first["total"] == 4


def test_paging_a_sorted_list_never_repeats_a_row(auth_client: TestClient) -> None:
    """Rows sharing a sort value need a tiebreaker, or paging can show one twice."""
    account = create_account(auth_client)
    for i in range(6):
        auth_client.post(
            "/api/transactions",
            json={
                "account_id": account["id"],
                "txn_date": "2025-06-01",
                "amount_cents": -500,  # every row identical on the sort column
                "description": f"ROW {i}",
            },
        )
    seen: list[str] = []
    for page in (1, 2, 3):
        seen += [t["id"] for t in _get(auth_client, sort="amount", page=page, page_size=2)["items"]]
    assert len(seen) == len(set(seen)) == 6


def test_unknown_sort_key_is_rejected(auth_client: TestClient) -> None:
    """Sort keys are allow-listed, so nothing user-supplied reaches the ORDER BY."""
    assert auth_client.get("/api/transactions", params={"sort": "id"}).status_code == 400
    assert auth_client.get("/api/transactions", params={"sort": "amount; DROP"}).status_code == 400
    assert (
        auth_client.get("/api/transactions", params={"sort": "amount", "dir": "up"}).status_code
        == 400
    )


def test_default_order_is_unchanged(auth_client: TestClient) -> None:
    """Callers that ask for no sort still get newest first."""
    _seed(auth_client)
    assert [t["txn_date"] for t in _get(auth_client)["items"]] == [
        "2025-06-05",
        "2025-06-03",
        "2025-06-02",
        "2025-06-01",
    ]


# ----------------------------------------------------------------------- filtering


def test_filter_by_description(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert _descriptions(_get(auth_client, f_description="wool")) == ["WOOLWORTHS METRO"]


def test_filter_by_account_and_category_name(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert _get(auth_client, f_account="savings")["total"] == 2
    assert _descriptions(_get(auth_client, f_category="rent")) == ["RENT PAYMENT"]


def test_filter_by_amount_matches_the_displayed_value(auth_client: TestClient) -> None:
    """Amounts are stored in cents, so "85.40" and "85" must both find -$85.40."""
    _seed(auth_client)
    assert _descriptions(_get(auth_client, f_amount="85.40")) == ["WOOLWORTHS METRO"]
    assert _descriptions(_get(auth_client, f_amount="$85")) == ["WOOLWORTHS METRO"]
    assert _get(auth_client, f_amount="1000")["total"] == 1  # -$1000.00 rent


def test_amount_filter_handles_cents_that_would_round_up(auth_client: TestClient) -> None:
    """Guards a Postgres-only break: casting 85.6 to an integer rounds there, which
    would render -$85.60 as "86.-40". Cheap to assert, invisible on SQLite."""
    account = create_account(auth_client)
    for amount in (-8560, -8599, -5, -100):
        auth_client.post(
            "/api/transactions",
            json={
                "account_id": account["id"],
                "txn_date": "2025-06-01",
                "amount_cents": amount,
                "description": f"ROW {amount}",
            },
        )
    assert _descriptions(_get(auth_client, f_amount="85.60")) == ["ROW -8560"]
    assert _descriptions(_get(auth_client, f_amount="85.99")) == ["ROW -8599"]
    assert _descriptions(_get(auth_client, f_amount="0.05")) == ["ROW -5"]
    assert _descriptions(_get(auth_client, f_amount="1.00")) == ["ROW -100"]


def test_amount_filter_of_only_punctuation_matches_nothing(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert _get(auth_client, f_amount="$")["total"] == 0


def test_filter_by_date_fragment(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert _get(auth_client, f_date="2025-06")["total"] == 4
    assert _descriptions(_get(auth_client, f_date="2025-06-03")) == ["RENT PAYMENT"]


def test_filters_combine_with_and(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert _get(auth_client, f_account="everyday", f_category="rent")["total"] == 1
    assert _get(auth_client, f_account="savings", f_category="rent")["total"] == 0


def test_filters_apply_before_pagination(auth_client: TestClient) -> None:
    _seed(auth_client)
    filtered = _get(auth_client, f_account="savings", page_size=1)
    assert filtered["total"] == 2  # the count reflects the filter, not the page
    assert len(filtered["items"]) == 1


def test_search_spans_every_displayed_column(auth_client: TestClient) -> None:
    """`q` used to look only at description and merchant."""
    _seed(auth_client)
    assert _get(auth_client, q="Savings")["total"] == 2  # account name
    assert _descriptions(_get(auth_client, q="Rent")) == ["RENT PAYMENT"]  # category name
    assert _get(auth_client, q="2025-06-05")["total"] == 1  # date
    assert _descriptions(_get(auth_client, q="85.40")) == ["WOOLWORTHS METRO"]  # amount


def test_sorting_and_filtering_compose_with_the_period(auth_client: TestClient) -> None:
    _seed(auth_client)
    result = _get(auth_client, period="fy:2024", sort="amount", dir="asc")
    assert [t["amount_cents"] for t in result["items"]] == [-100000, -8540, -900, 120]
    assert _get(auth_client, period="fy:2019")["total"] == 0
