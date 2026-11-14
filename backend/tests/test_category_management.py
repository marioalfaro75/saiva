"""Adding, renaming and removing categories.

Two things carry the weight here.

Renaming must be a LABEL only. Four features found a built-in category by its name --
transfer detection ("Internal transfers"), the categoriser's fallback and the advisor
("Uncategorised"), and the benchmarks (by group) -- so a rename would have quietly
switched each of them off. They now match on `system_key`, the name the category was
created with, and the tests below rename each one and prove the feature still works.

Removing must lose nothing silently. Its transactions go where the household says (or
become uncategorised), its rules follow them, its budget is removed, and the two
categories the app files into itself cannot be removed at all.
"""

from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.db import SessionLocal, engine
from app.dbinit import _record_category_keys
from app.services import advisor, benchmarks, periods
from app.services.categorise import build_categoriser


def _categories(client: TestClient) -> list[dict]:
    return client.get("/api/categories").json()


def _by_name(client: TestClient, name: str) -> dict:
    return next(c for c in _categories(client) if c["name"] == name)


def _rename(client: TestClient, category_id: str, name: str):  # type: ignore[no-untyped-def]
    return client.patch(f"/api/categories/{category_id}", json={"name": name})


def _txn(client: TestClient, account_id: str, cents: int, desc: str, date: str) -> dict:
    resp = client.post(
        "/api/transactions",
        json={"account_id": account_id, "txn_date": date, "amount_cents": cents, "description": desc},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_in(client: TestClient, txn_ids: list[str], category_id: str | None) -> None:
    resp = client.post(
        "/api/transactions/bulk-categorise", json={"ids": txn_ids, "category_id": category_id}
    )
    assert resp.status_code == 200, resp.text


def _household_id(category_id: str) -> str:
    with SessionLocal() as db:
        category = db.get(models.Category, category_id)
        assert category is not None
        return category.household_id


def _category_of(txn_id: str) -> str | None:
    with SessionLocal() as db:
        txn = db.get(models.Transaction, txn_id)
        assert txn is not None
        return txn.category_id


# --------------------------------------------------------------------------------------
# What the list says about each category
# --------------------------------------------------------------------------------------


def test_built_in_categories_carry_their_key_and_the_two_the_app_needs_are_protected(
    auth_client: TestClient,
) -> None:
    cats = _categories(auth_client)
    assert all(c["system_key"] == c["name"] for c in cats), "a new household keys every built-in"
    protected = sorted(c["name"] for c in cats if c["protected"])
    assert protected == ["Internal transfers", "Uncategorised"]


# --------------------------------------------------------------------------------------
# Renaming
# --------------------------------------------------------------------------------------


def test_renaming_changes_the_label_and_keeps_the_key(auth_client: TestClient) -> None:
    fuel = _by_name(auth_client, "Fuel")
    resp = _rename(auth_client, fuel["id"], "  Petrol   and  diesel ")

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Petrol and diesel", "whitespace is tidied"
    assert resp.json()["system_key"] == "Fuel"
    assert _by_name(auth_client, "Petrol and diesel")["id"] == fuel["id"]


def test_a_name_already_in_use_is_refused_whatever_its_case(auth_client: TestClient) -> None:
    fuel = _by_name(auth_client, "Fuel")

    resp = _rename(auth_client, fuel["id"], "PARKING")

    assert resp.status_code == 409
    assert "Parking" in resp.json()["detail"] or "PARKING" in resp.json()["detail"]


def test_changing_only_the_case_of_its_own_name_is_allowed(auth_client: TestClient) -> None:
    fuel = _by_name(auth_client, "Fuel")

    assert _rename(auth_client, fuel["id"], "FUEL").status_code == 200


def test_a_blank_name_is_refused(auth_client: TestClient) -> None:
    fuel = _by_name(auth_client, "Fuel")

    assert _rename(auth_client, fuel["id"], "   ").status_code == 422


def test_an_unknown_category_is_not_found(auth_client: TestClient) -> None:
    assert _rename(auth_client, "no-such-category", "Anything").status_code == 404
    assert auth_client.delete("/api/categories/no-such-category").status_code == 404


# --------------------------------------------------------------------------------------
# A rename is only a label: the four features that look for a built-in still find it
# --------------------------------------------------------------------------------------


def test_transfer_detection_still_files_transfers_after_internal_transfers_is_renamed(
    auth_client: TestClient,
) -> None:
    transfers = _by_name(auth_client, "Internal transfers")
    assert _rename(auth_client, transfers["id"], "Between my accounts").status_code == 200
    everyday = create_account(auth_client, "Everyday", "everyday")
    savings = create_account(auth_client, "Savings", "savings")
    out = _txn(auth_client, everyday["id"], -50000, "TRANSFER TO SAVINGS", "2025-06-10")
    into = _txn(auth_client, savings["id"], 50000, "TRANSFER FROM EVERYDAY", "2025-06-10")

    assert auth_client.post("/api/transactions/detect-transfers").json()["linked"] == 2
    assert _category_of(out["id"]) == transfers["id"]
    assert _category_of(into["id"]) == transfers["id"]


def test_the_categoriser_still_falls_back_to_uncategorised_after_it_is_renamed(
    auth_client: TestClient,
) -> None:
    bucket = _by_name(auth_client, "Uncategorised")
    assert _rename(auth_client, bucket["id"], "To sort").status_code == 200

    with SessionLocal() as db:
        categoriser = build_categoriser(db, _household_id(bucket["id"]))

    assert categoriser.uncategorised_id == bucket["id"]


def test_benchmarks_still_count_a_renamed_group(auth_client: TestClient, today: dt.date) -> None:
    groceries = _by_name(auth_client, "Groceries")
    supermarkets = _by_name(auth_client, "Supermarkets")
    account = create_account(auth_client)
    last_month = (today.replace(day=1) - dt.timedelta(days=10)).isoformat()
    spend = _txn(auth_client, account["id"], -12345, "SOME GROCER", last_month)
    _put_in(auth_client, [spend["id"]], supermarkets["id"])
    assert _rename(auth_client, groceries["id"], "Food shopping").status_code == 200

    with SessionLocal() as db:
        yours = benchmarks._your_weekly_by_category(db, _household_id(groceries["id"]), today)

    assert yours.get("Groceries", 0) > 0, f"the renamed group fell out of the benchmarks: {yours}"


def test_a_group_the_household_created_has_no_benchmark_to_meet(
    auth_client: TestClient, today: dt.date
) -> None:
    # Keyed on system_key, so a new group called "Groceries" after the real one was
    # renamed is not counted as groceries twice.
    groceries = _by_name(auth_client, "Groceries")
    assert _rename(auth_client, groceries["id"], "Food shopping").status_code == 200
    mine = auth_client.post("/api/categories", json={"name": "Groceries"}).json()
    child = auth_client.post("/api/categories", json={"name": "Market", "parent_id": mine["id"]}).json()
    account = create_account(auth_client)
    last_month = (today.replace(day=1) - dt.timedelta(days=10)).isoformat()
    spend = _txn(auth_client, account["id"], -5000, "FARMERS MARKET", last_month)
    _put_in(auth_client, [spend["id"]], child["id"])

    with SessionLocal() as db:
        yours = benchmarks._your_weekly_by_category(db, _household_id(mine["id"]), today)

    assert "Groceries" not in yours


def test_the_advisor_still_sees_uncategorised_spending_after_the_bucket_is_renamed(
    auth_client: TestClient,
) -> None:
    bucket = _by_name(auth_client, "Uncategorised")
    account = create_account(auth_client)
    spend = _txn(auth_client, account["id"], -4200, "MYSTERY SHOP", "2025-06-12")
    _put_in(auth_client, [spend["id"]], bucket["id"])
    assert _rename(auth_client, bucket["id"], "To sort").status_code == 200

    with SessionLocal() as db:
        household = db.get(models.Household, _household_id(bucket["id"]))
        assert household is not None
        window = periods.ResolvedPeriod(
            start=dt.date(2025, 6, 1),
            end=dt.date(2025, 6, 30),
            label="June 2025",
            as_at=dt.date(2025, 6, 30),
            is_current=False,
        )
        context = advisor.build_context(db, household, "full", window)

    assert "UNCATEGORISED IN" in context and "1 transactions" in context, context[-600:]


# --------------------------------------------------------------------------------------
# Adding
# --------------------------------------------------------------------------------------


def test_a_new_category_takes_its_groups_kind(auth_client: TestClient) -> None:
    income = _by_name(auth_client, "Income")
    resp = auth_client.post(
        "/api/categories", json={"name": "Side hustle", "parent_id": income["id"], "kind": "expense"}
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "income", "an income group cannot quietly hold an expense"
    assert resp.json()["system_key"] is None
    assert resp.json()["protected"] is False


def test_a_new_category_goes_last(auth_client: TestClient) -> None:
    pets = _by_name(auth_client, "Pets")
    created = auth_client.post("/api/categories", json={"name": "Pet insurance", "parent_id": pets["id"]})

    assert created.json()["sort"] > max(c["sort"] for c in _categories(auth_client) if c["id"] != created.json()["id"])


def test_a_new_group_can_be_added(auth_client: TestClient) -> None:
    resp = auth_client.post("/api/categories", json={"name": "Boat", "kind": "expense"})

    assert resp.status_code == 201
    assert resp.json()["parent_id"] is None


def test_a_category_cannot_go_inside_another_category(auth_client: TestClient) -> None:
    fuel = _by_name(auth_client, "Fuel")

    resp = auth_client.post("/api/categories", json={"name": "Premium", "parent_id": fuel["id"]})

    assert resp.status_code == 400


def test_a_new_category_cannot_reuse_a_name(auth_client: TestClient) -> None:
    resp = auth_client.post("/api/categories", json={"name": "supermarkets"})

    assert resp.status_code == 409


# --------------------------------------------------------------------------------------
# Removing
# --------------------------------------------------------------------------------------


def _coffee_with_everything(client: TestClient) -> tuple[dict, list[dict], dict, dict]:
    """Coffee with two transactions, a rule and a budget pointing at it."""
    coffee = _by_name(client, "Coffee")
    account = create_account(client)
    txns = [
        _txn(client, account["id"], -550, "CAMPOS COFFEE", "2025-06-01"),
        _txn(client, account["id"], -480, "CAMPOS COFFEE", "2025-06-02"),
    ]
    _put_in(client, [t["id"] for t in txns], coffee["id"])
    rule = client.post(
        "/api/rules", json={"match_type": "contains", "pattern": "CAMPOS", "category_id": coffee["id"]}
    ).json()
    budget = client.post("/api/budgets", json={"category_id": coffee["id"], "limit_cents": 5000}).json()
    return coffee, txns, rule, budget


def test_usage_says_what_removing_would_touch(auth_client: TestClient) -> None:
    coffee, _txns, _rule, _budget = _coffee_with_everything(auth_client)
    usage = auth_client.get(f"/api/categories/{coffee['id']}/usage").json()

    assert usage["transactions"] == 2
    assert usage["rules"] >= 1
    assert usage["budgets"] == 1
    assert usage["subcategories"] == 0


def test_removing_moves_transactions_and_rules_and_removes_the_budget(
    auth_client: TestClient,
) -> None:
    coffee, txns, rule, _budget = _coffee_with_everything(auth_client)
    takeaway = _by_name(auth_client, "Takeaway")
    rules_before = auth_client.get(f"/api/categories/{coffee['id']}/usage").json()["rules"]

    resp = auth_client.delete(f"/api/categories/{coffee['id']}", params={"move_to": takeaway["id"]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "moved_transactions": 2,
        "moved_rules": rules_before,
        "removed_rules": 0,
        "removed_budgets": 1,
    }
    assert all(_category_of(t["id"]) == takeaway["id"] for t in txns)
    rules = {r["id"]: r for r in auth_client.get("/api/rules").json()}
    assert rules[rule["id"]]["category_id"] == takeaway["id"], "the rule followed its transactions"
    assert all(b["category_id"] != coffee["id"] for b in auth_client.get("/api/budgets").json())
    assert all(c["id"] != coffee["id"] for c in _categories(auth_client))


def test_removing_with_nowhere_to_go_uncategorises_and_drops_its_rules(
    auth_client: TestClient,
) -> None:
    coffee, txns, rule, _budget = _coffee_with_everything(auth_client)

    resp = auth_client.delete(f"/api/categories/{coffee['id']}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["moved_transactions"] == 2
    assert resp.json()["moved_rules"] == 0
    assert resp.json()["removed_rules"] >= 1
    assert all(_category_of(t["id"]) is None for t in txns)
    assert rule["id"] not in {r["id"] for r in auth_client.get("/api/rules").json()}


def test_removing_leaves_every_other_category_alone(auth_client: TestClient) -> None:
    _coffee_with_everything(auth_client)
    supermarkets = _by_name(auth_client, "Supermarkets")
    account = create_account(auth_client, "Other")
    other = _txn(auth_client, account["id"], -9000, "WOOLWORTHS", "2025-06-03")
    _put_in(auth_client, [other["id"]], supermarkets["id"])
    coffee = _by_name(auth_client, "Coffee")

    auth_client.delete(f"/api/categories/{coffee['id']}")

    assert _category_of(other["id"]) == supermarkets["id"]


def test_the_two_categories_the_app_files_into_cannot_be_removed_even_once_renamed(
    auth_client: TestClient,
) -> None:
    for name, new_name in (("Internal transfers", "Moves"), ("Uncategorised", "To sort")):
        category = _by_name(auth_client, name)
        assert _rename(auth_client, category["id"], new_name).status_code == 200

        resp = auth_client.delete(f"/api/categories/{category['id']}")

        assert resp.status_code == 409, f"{name} was removable after a rename"
        assert "rename" in resp.json()["detail"].lower()


def test_a_group_that_still_has_categories_cannot_be_removed(auth_client: TestClient) -> None:
    pets = _by_name(auth_client, "Pets")

    assert auth_client.delete(f"/api/categories/{pets['id']}").status_code == 409

    for child in [c for c in _categories(auth_client) if c["parent_id"] == pets["id"]]:
        assert auth_client.delete(f"/api/categories/{child['id']}").status_code == 200
    assert auth_client.delete(f"/api/categories/{pets['id']}").status_code == 200


def test_moving_to_itself_or_to_a_category_that_does_not_exist_is_refused(
    auth_client: TestClient,
) -> None:
    coffee = _by_name(auth_client, "Coffee")

    assert auth_client.delete(
        f"/api/categories/{coffee['id']}", params={"move_to": coffee["id"]}
    ).status_code == 400
    assert auth_client.delete(
        f"/api/categories/{coffee['id']}", params={"move_to": "no-such-category"}
    ).status_code == 400
    assert any(c["id"] == coffee["id"] for c in _categories(auth_client)), "nothing was removed"


def test_a_viewer_can_neither_rename_nor_remove(auth_client: TestClient) -> None:
    coffee = _by_name(auth_client, "Coffee")
    auth_client.post(
        "/api/household/users",
        json={"name": "Viewer", "email": "viewer@example.com", "password": "viewerpass99", "role": "viewer"},
    )
    from fastapi.testclient import TestClient as TC

    from app.main import app

    with TC(app) as viewer:
        viewer.headers["X-CSRF-Token"] = viewer.get("/api/auth/csrf").json()["csrf_token"]
        login = viewer.post(
            "/api/auth/login", json={"email": "viewer@example.com", "password": "viewerpass99"}
        )
        viewer.headers["X-CSRF-Token"] = login.json()["csrf_token"]

        assert viewer.patch(f"/api/categories/{coffee['id']}", json={"name": "Caffeine"}).status_code == 403
        assert viewer.delete(f"/api/categories/{coffee['id']}").status_code == 403
        assert viewer.post("/api/categories", json={"name": "Snacks"}).status_code == 403


# --------------------------------------------------------------------------------------
# Upgrading a household created before categories had keys
# --------------------------------------------------------------------------------------


def test_startup_keys_existing_built_ins_once_and_never_overwrites_a_key(
    auth_client: TestClient,
) -> None:
    # A household from before revision 0013: every key empty.
    with SessionLocal() as db:
        for c in db.execute(select(models.Category)).scalars():
            c.system_key = None
        db.add(
            models.Category(
                household_id=db.execute(select(models.Household.id)).scalar_one(),
                name="Bike parts",
                is_system=False,
            )
        )
        db.commit()

    _record_category_keys(engine)
    cats = {c["name"]: c for c in _categories(auth_client)}
    assert cats["Internal transfers"]["system_key"] == "Internal transfers"
    assert cats["Bike parts"]["system_key"] is None, "a household's own category has no key"

    # Renamed afterwards, then the app restarts: the key must not follow the new name.
    assert _rename(auth_client, cats["Internal transfers"]["id"], "Moves").status_code == 200
    _record_category_keys(engine)
    assert _by_name(auth_client, "Moves")["system_key"] == "Internal transfers"


# --------------------------------------------------------------------------------------
# Found in review
# --------------------------------------------------------------------------------------


def test_loading_demo_data_after_a_rename_still_files_it_in_the_built_in_category(
    auth_client: TestClient,
) -> None:
    # The demo seeder looked categories up by name too -- a sixth lookup the first pass
    # missed. Renamed "Supermarkets" left every demo grocery shop uncategorised and its
    # budget never made.
    supermarkets = _by_name(auth_client, "Supermarkets")
    assert _rename(auth_client, supermarkets["id"], "Grocery stores").status_code == 200

    resp = auth_client.post("/api/admin/seed-demo")
    assert resp.status_code in (200, 201), resp.text

    with SessionLocal() as db:
        grocers = (
            db.execute(
                select(models.Transaction).where(
                    models.Transaction.raw_description.ilike("%WOOLWORTHS%")
                )
            )
            .scalars()
            .all()
        )
        budgeted = set(db.execute(select(models.Budget.category_id)).scalars())
    assert grocers, "the demo should include supermarket shops"
    assert all(t.category_id == supermarkets["id"] for t in grocers)
    assert supermarkets["id"] in budgeted


def test_names_are_compared_the_same_way_on_both_sides(auth_client: TestClient) -> None:
    # The database's lower() and Python's lower() disagree on some letters, so an exact
    # duplicate got through. Now both sides are folded in Python.
    assert auth_client.post("/api/categories", json={"name": "İzmir"}).status_code == 201
    assert auth_client.post("/api/categories", json={"name": "İzmir"}).status_code == 409
    assert auth_client.post("/api/categories", json={"name": "Straße"}).status_code == 201
    assert auth_client.post("/api/categories", json={"name": "STRASSE"}).status_code == 409


def test_a_name_cannot_hide_behind_characters_that_print_as_nothing(
    auth_client: TestClient,
) -> None:
    assert auth_client.post("/api/categories", json={"name": "​"}).status_code == 422
    assert auth_client.post("/api/categories", json={"name": "Fuel​"}).status_code == 409
    assert auth_client.post("/api/categories", json={"name": "Fu\u0000el"}).status_code == 409
    # The seeded "Restaurants/Cafés", typed with a combining accent instead.
    assert (
        auth_client.post("/api/categories", json={"name": "Restaurants/Cafés"}).status_code
        == 409
    )
