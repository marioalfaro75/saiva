"""Rules take effect when they change, and say beforehand what they will do.

Asked for: typing a pattern shows the transactions it catches; a rule made is listed;
making a rule files its transactions and removing one puts them back. Before this, a
rule changed nothing until "Apply now" (which only filled blanks), deleting one left
everything it had filed, and the preview was a count and three names.

The line every test here draws: a rule decides blank and automatic categories -- the
learner's guesses and other rules' work -- and never one a person chose or locked.
"""

from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.db import SessionLocal, engine
from app.dbinit import _mark_automatic_categories
from app.services.categorise import (
    ML_MAX_CONFIDENCE,
    RULE_CONFIDENCE,
    Categoriser,
    preview_rule,
)
from app.services.ml import NaiveBayesCategoriser

GUESS = 0.7  # a confidence only the learner produces


def _add(  # type: ignore[no-untyped-def]
    client: TestClient,
    account_id: str,
    desc: str,
    date: str = "2025-06-01",
    cents: int = -1000,
    **extra,
):
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": date,
            "amount_cents": cents,
            "description": desc,
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _cat(client: TestClient, name: str) -> str:
    return next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name)


def _rule(client: TestClient, pattern: str, category_id: str, match_type: str = "contains") -> dict:
    resp = client.post(
        "/api/rules",
        json={"match_type": match_type, "pattern": pattern, "category_id": category_id},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _state(txn_id: str) -> tuple[str | None, float | None]:
    with SessionLocal() as db:
        t = db.get(models.Transaction, txn_id)
        assert t is not None
        return t.category_id, t.confidence


def _set(txn_id: str, **fields: object) -> None:
    with SessionLocal() as db:
        t = db.get(models.Transaction, txn_id)
        assert t is not None
        for key, value in fields.items():
            setattr(t, key, value)
        db.commit()


def _choose(client: TestClient, txn_id: str, category_id: str) -> None:
    """A person picks the category, the way the transactions table does."""
    resp = client.post(
        f"/api/transactions/{txn_id}/recategorise",
        json={"category_id": category_id, "scope": "none"},
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------------------
# The preview
# --------------------------------------------------------------------------------------


def test_the_preview_lists_each_transaction_and_what_the_rule_would_do(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    coffee, dining, groceries = (
        _cat(auth_client, n) for n in ("Coffee", "Restaurants/Cafés", "Supermarkets")
    )
    blank = _add(auth_client, account, "ZEBRA CAFE NORTH", date="2025-06-05")
    chosen = _add(auth_client, account, "ZEBRA CAFE SOUTH", date="2025-06-04")
    _choose(auth_client, chosen, dining)
    locked = _add(auth_client, account, "ZEBRA CAFE EAST", date="2025-06-03")
    _set(locked, category_locked=True)
    guessed = _add(auth_client, account, "ZEBRA CAFE WEST", date="2025-06-02")
    _set(guessed, category_id=coffee, confidence=GUESS)
    # An earlier rule of the household's own catches this one first.
    _rule(auth_client, "zebra cafe central", groceries)
    earlier = _add(auth_client, account, "ZEBRA CAFE CENTRAL", date="2025-06-01")
    transfer = _add(auth_client, account, "ZEBRA CAFE TRANSFER", date="2025-06-06")
    _set(transfer, is_transfer=True)

    resp = auth_client.post(
        "/api/rules/preview",
        json={"match_type": "contains", "pattern": "zebra cafe", "category_id": coffee},
    )
    assert resp.status_code == 200, resp.text
    p = resp.json()

    # Transfers are filed by transfer detection, and no rule reaches them.
    assert p["matched"] == 5
    assert [r["id"] for r in p["rows"]] == [blank, chosen, locked, guessed, earlier]
    outcomes = {r["id"]: r["outcome"] for r in p["rows"]}
    assert outcomes == {
        blank: "change",
        chosen: "kept",
        locked: "locked",
        guessed: "same",
        earlier: "other_rule",
    }
    assert (p["will_change"], p["kept"], p["locked"], p["already"], p["other_rule"]) == (
        1,
        1,
        1,
        1,
        1,
    )
    first = next(r for r in p["rows"] if r["id"] == earlier)
    assert first["other_rule_pattern"] == "zebra cafe central"
    assert first["other_rule_category_name"] == "Supermarkets"
    assert next(r for r in p["rows"] if r["id"] == chosen)["category_name"] == "Restaurants/Cafés"
    # Distinct names, not the same merchant three times over.
    assert len(p["samples"]) == len(set(p["samples"]))


def test_the_preview_works_before_a_category_is_chosen(auth_client: TestClient) -> None:
    account = create_account(auth_client)["id"]
    _add(auth_client, account, "ZEBRA CAFE NORTH")
    p = auth_client.post(
        "/api/rules/preview", json={"match_type": "contains", "pattern": "zebra"}
    ).json()
    assert p["matched"] == 1
    assert p["rows"][0]["outcome"] == "change"


def test_the_preview_refuses_a_category_that_is_not_the_households(
    auth_client: TestClient,
) -> None:
    resp = auth_client.post(
        "/api/rules/preview",
        json={"match_type": "contains", "pattern": "zebra", "category_id": "not-a-category"},
    )
    assert resp.status_code in (400, 404)


def test_the_preview_caps_its_rows_but_not_its_count(auth_client: TestClient) -> None:
    account = create_account(auth_client)["id"]
    for day in (1, 2, 3):
        _add(auth_client, account, f"ZEBRA CAFE {day}", date=f"2025-06-0{day}")
    with SessionLocal() as db:
        household = db.execute(select(models.Household.id)).scalar_one()
        result = preview_rule(db, household, "contains", "zebra", row_limit=2)
    assert result.matched == 3
    assert [r.txn.raw_description for r in result.rows] == ["ZEBRA CAFE 3", "ZEBRA CAFE 2"]


# --------------------------------------------------------------------------------------
# Making a rule
# --------------------------------------------------------------------------------------


def test_making_a_rule_files_blank_and_guessed_transactions_but_not_a_persons_choice(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    coffee, dining = _cat(auth_client, "Coffee"), _cat(auth_client, "Restaurants/Cafés")
    blank = _add(auth_client, account, "ZEBRA CAFE 1")
    guessed = _add(auth_client, account, "ZEBRA CAFE 2")
    _set(guessed, category_id=dining, confidence=GUESS)
    chosen = _add(auth_client, account, "ZEBRA CAFE 3")
    _choose(auth_client, chosen, dining)
    added_with_category = _add(auth_client, account, "ZEBRA CAFE 4", category_id=dining)
    locked = _add(auth_client, account, "ZEBRA CAFE 5")
    _set(locked, category_locked=True)
    transfer = _add(auth_client, account, "ZEBRA CAFE 6")
    _set(transfer, is_transfer=True)

    made = _rule(auth_client, "zebra cafe", coffee)

    assert made["categorised"] == 2
    assert made["uncategorised"] == 0
    assert _state(blank) == (coffee, RULE_CONFIDENCE)
    assert _state(guessed) == (coffee, RULE_CONFIDENCE)
    assert _state(chosen)[0] == dining
    assert _state(added_with_category)[0] == dining
    assert _state(locked) == (None, None)
    assert _state(transfer) == (None, None)


def test_a_rule_made_in_the_categorise_dialog_files_the_rest_too(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    coffee = _cat(auth_client, "Coffee")
    first = _add(auth_client, account, "ZEBRA ROASTERS")
    other = _add(auth_client, account, "ZEBRA ROASTERS")
    resp = auth_client.post(
        f"/api/transactions/{first}/recategorise",
        json={"category_id": coffee, "scope": "none", "make_rule": True},
    )
    assert resp.status_code == 200, resp.text
    # The one picked, plus the one the new rule then caught.
    assert resp.json()["updated_count"] == 2
    assert _state(first) == (coffee, 1.0)
    assert _state(other) == (coffee, RULE_CONFIDENCE)


# --------------------------------------------------------------------------------------
# Removing, editing, switching off
# --------------------------------------------------------------------------------------


def test_deleting_a_rule_puts_back_what_it_filed_and_nothing_else(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    coffee, dining = _cat(auth_client, "Coffee"), _cat(auth_client, "Restaurants/Cafés")
    blank = _add(auth_client, account, "ZEBRA CAFE 1")
    guessed = _add(auth_client, account, "ZEBRA CAFE 2")
    _set(guessed, category_id=dining, confidence=GUESS)
    chosen = _add(auth_client, account, "ZEBRA CAFE 3")
    _choose(auth_client, chosen, coffee)
    rule = _rule(auth_client, "zebra cafe", coffee)
    # A guess in the rule's category that the rule never matched stays a guess.
    elsewhere = _add(auth_client, account, "OKAPI BEANS")
    _set(elsewhere, category_id=coffee, confidence=GUESS)

    resp = auth_client.delete(f"/api/rules/{rule['id']}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"categorised": 0, "uncategorised": 2}
    assert _state(blank) == (None, None)
    # The rule had taken this over from the guess; with the rule gone it is
    # Uncategorised, for review, rather than a guess revived from before.
    assert _state(guessed) == (None, None)
    assert _state(chosen) == (coffee, 1.0)
    assert _state(elsewhere) == (coffee, GUESS)
    assert all(r["id"] != rule["id"] for r in auth_client.get("/api/rules").json())


def test_deleting_a_rule_hands_its_transactions_to_the_next_rule_that_matches(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    coffee, groceries = _cat(auth_client, "Coffee"), _cat(auth_client, "Supermarkets")
    specific = _rule(auth_client, "zebra cafe", coffee)  # older: tried first
    _rule(auth_client, "zebra", groceries)
    txn = _add(auth_client, account, "ZEBRA CAFE 1")
    assert _state(txn)[0] == coffee

    resp = auth_client.delete(f"/api/rules/{specific['id']}")

    assert resp.json() == {"categorised": 1, "uncategorised": 0}
    assert _state(txn) == (groceries, RULE_CONFIDENCE)


def test_a_households_rule_beats_a_built_in_one_and_hands_back_when_deleted(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    takeaway = _cat(auth_client, "Takeaway")
    txn = _add(auth_client, account, "WOOLWORTHS 1234 SYDNEY")
    built_in, how = _state(txn)
    assert built_in is not None and how == RULE_CONFIDENCE, "a starter rule should file it"

    rule = _rule(auth_client, "woolworths", takeaway)
    assert rule["categorised"] == 1
    assert _state(txn)[0] == takeaway

    auth_client.delete(f"/api/rules/{rule['id']}")
    assert _state(txn) == (built_in, RULE_CONFIDENCE)


def test_editing_switching_off_and_on_move_what_the_rule_matches(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    coffee, groceries = _cat(auth_client, "Coffee"), _cat(auth_client, "Supermarkets")
    zebra_1 = _add(auth_client, account, "ZEBRA ONE")
    zebra_2 = _add(auth_client, account, "ZEBRA TWO")
    okapi = _add(auth_client, account, "OKAPI ONE")
    rule = _rule(auth_client, "zebra", coffee)
    patch = lambda body: auth_client.patch(f"/api/rules/{rule['id']}", json=body).json()  # noqa: E731

    changed = patch({"pattern": "okapi"})
    assert (changed["categorised"], changed["uncategorised"]) == (1, 2)
    assert _state(zebra_1) == (None, None)
    assert _state(zebra_2) == (None, None)
    assert _state(okapi)[0] == coffee

    moved = patch({"category_id": groceries})
    assert (moved["categorised"], moved["uncategorised"]) == (1, 0)
    assert _state(okapi)[0] == groceries

    off = patch({"is_active": False})
    assert (off["categorised"], off["uncategorised"]) == (0, 1)
    assert _state(okapi) == (None, None)

    on = patch({"is_active": True})
    assert (on["categorised"], on["uncategorised"]) == (1, 0)
    assert _state(okapi) == (groceries, RULE_CONFIDENCE)


def test_an_explicit_null_in_an_edit_leaves_the_field_alone(auth_client: TestClient) -> None:
    coffee = _cat(auth_client, "Coffee")
    rule = _rule(auth_client, "zebra", coffee)
    resp = auth_client.patch(f"/api/rules/{rule['id']}", json={"pattern": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pattern"] == "zebra"


# --------------------------------------------------------------------------------------
# Order, and what a confidence says
# --------------------------------------------------------------------------------------


def test_rules_at_the_same_priority_are_tried_oldest_first(auth_client: TestClient) -> None:
    account = create_account(auth_client)["id"]
    coffee, groceries = _cat(auth_client, "Coffee"), _cat(auth_client, "Supermarkets")
    _rule(auth_client, "zebra", coffee)
    _rule(auth_client, "zebra", groceries)
    # On import (here, adding one by hand without a category) ...
    txn = _add(auth_client, account, "ZEBRA ONE")
    assert _state(txn)[0] == coffee
    # ... and in a preview, the older rule is the one that goes first.
    p = auth_client.post(
        "/api/rules/preview",
        json={"match_type": "contains", "pattern": "zebra", "category_id": groceries},
    ).json()
    assert p["rows"][0]["outcome"] == "other_rule"
    assert p["rows"][0]["other_rule_category_name"] == "Coffee"


def test_a_guess_never_scores_like_a_rule_or_a_person() -> None:
    ml = NaiveBayesCategoriser()
    ml.train([("zebra cafe flat white", "coffee")] * 40 + [("okapi fuel unleaded", "fuel")] * 40)
    predicted, raw = ml.predict("zebra cafe flat white")
    assert predicted == "coffee"
    assert raw > ML_MAX_CONFIDENCE, "the learner must be confident here for this to test the cap"

    result = Categoriser([], ml, None).categorise("zebra cafe flat white")

    assert result.source == "ml"
    assert result.confidence <= ML_MAX_CONFIDENCE < RULE_CONFIDENCE


# --------------------------------------------------------------------------------------
# Found in review
# --------------------------------------------------------------------------------------


def test_a_new_rule_leaves_what_an_earlier_rule_wins_where_it_is(auth_client: TestClient) -> None:
    # An older rule that never filed a row (before this release a rule only filled blanks,
    # and only on "Apply now") still goes first. Saving a new rule used to hand such a row
    # to the older rule -- moving it somewhere the preview never mentioned -- and the
    # message then said it had moved to the NEW rule's category.
    account = create_account(auth_client)["id"]
    coffee, groceries = _cat(auth_client, "Coffee"), _cat(auth_client, "Supermarkets")
    _rule(auth_client, "zebra", groceries)  # older
    guessed = _add(auth_client, account, "ZEBRA CAFE 1")
    _set(guessed, category_id=coffee, confidence=GUESS)
    blank = _add(auth_client, account, "ZEBRA CAFE 2")
    _set(blank, category_id=None, confidence=None)

    p = auth_client.post(
        "/api/rules/preview",
        json={"match_type": "contains", "pattern": "zebra cafe", "category_id": coffee},
    ).json()
    assert {r["id"]: r["outcome"] for r in p["rows"]} == {guessed: "same", blank: "other_rule"}

    made = _rule(auth_client, "zebra cafe", coffee)

    assert (made["categorised"], made["uncategorised"]) == (0, 0)
    assert _state(guessed) == (coffee, GUESS)
    assert _state(blank) == (None, None)


def test_deleting_a_rule_leaves_a_guess_it_never_filed(auth_client: TestClient) -> None:
    account = create_account(auth_client)["id"]
    coffee, groceries = _cat(auth_client, "Coffee"), _cat(auth_client, "Supermarkets")
    _rule(auth_client, "zebra", groceries)  # older, wins
    rule = _rule(auth_client, "zebra cafe", coffee)
    guessed = _add(auth_client, account, "ZEBRA CAFE 1")
    _set(guessed, category_id=coffee, confidence=GUESS)

    resp = auth_client.delete(f"/api/rules/{rule['id']}").json()

    assert resp == {"categorised": 0, "uncategorised": 0}
    assert _state(guessed) == (coffee, GUESS)


def test_a_pattern_that_can_never_match_is_refused_everywhere(auth_client: TestClient) -> None:
    coffee = _cat(auth_client, "Coffee")
    bad = {"match_type": "regex", "pattern": "campos("}
    assert auth_client.post("/api/rules/preview", json=bad).status_code == 422
    made = auth_client.post("/api/rules", json={**bad, "category_id": coffee})
    assert made.status_code == 422
    assert "regular expression" in made.json()["detail"]

    rule = _rule(auth_client, "campos", coffee)
    assert (
        auth_client.patch(
            f"/api/rules/{rule['id']}", json={"match_type": "regex", "pattern": "(("}
        ).status_code
        == 422
    )
    # Switching an existing pattern to regex is checked against that pattern too.
    bracket = _rule(auth_client, "[x", coffee)
    assert (
        auth_client.patch(f"/api/rules/{bracket['id']}", json={"match_type": "regex"}).status_code
        == 422
    )
    assert (
        auth_client.post(
            "/api/rules", json={"match_type": "regex", "pattern": "^campos", "category_id": coffee}
        ).status_code
        == 201
    )


def test_demo_data_is_marked_automatic_so_rules_can_re_file_it(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/seed-demo").status_code in (200, 201)
    with SessionLocal() as db:
        scores = {
            t.confidence
            for t in db.execute(select(models.Transaction)).scalars()
            if t.category_id is not None and not t.is_transfer
        }
    assert scores == {ML_MAX_CONFIDENCE}

    takeaway = _cat(auth_client, "Takeaway")
    p = auth_client.post(
        "/api/rules/preview",
        json={"match_type": "contains", "pattern": "coffee", "category_id": takeaway},
    ).json()
    assert p["matched"] > 0
    assert p["kept"] == 0


def test_the_other_half_of_a_deleted_transfer_leaves_internal_transfers(
    auth_client: TestClient,
) -> None:
    everyday = create_account(auth_client)["id"]
    savings = create_account(auth_client, name="Savings", type_="savings")["id"]
    out_id = _add(auth_client, everyday, "TRANSFER TO SAVINGS 99", cents=-50000)
    in_id = _add(auth_client, savings, "TRANSFER FROM EVERYDAY 99", cents=50000)
    assert auth_client.post("/api/transactions/detect-transfers").status_code == 200
    internal = _cat(auth_client, "Internal transfers")
    with SessionLocal() as db:
        legs = [db.get(models.Transaction, i) for i in (out_id, in_id)]
        assert all(t is not None and t.is_transfer for t in legs), "pair should be detected"
        assert all(t is not None and t.category_id == internal for t in legs)

    assert auth_client.delete(f"/api/transactions/{out_id}").status_code == 204

    assert _state(in_id) == (None, None)
    with SessionLocal() as db:
        survivor = db.get(models.Transaction, in_id)
        assert survivor is not None and survivor.is_transfer is False


def test_unticking_transfer_takes_it_out_of_internal_transfers_unless_a_person_chose_it(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)["id"]
    internal = _cat(auth_client, "Internal transfers")
    detected = _add(auth_client, account, "ZEBRA SWEEP 1")
    _set(detected, is_transfer=True, category_id=internal, confidence=None)
    chosen = _add(auth_client, account, "ZEBRA SWEEP 2")
    _choose(auth_client, chosen, internal)
    _set(chosen, is_transfer=True)

    for txn in (detected, chosen):
        resp = auth_client.patch(f"/api/transactions/{txn}", json={"is_transfer": False})
        assert resp.status_code == 200, resp.text

    assert _state(detected) == (None, None)
    assert _state(chosen) == (internal, 1.0)


def test_on_upgrade_old_guesses_and_unscored_imports_become_automatic(
    auth_client: TestClient,
) -> None:
    # Before guesses were capped, a confident one was stored as exactly 1.0 -- a person's
    # mark -- and the demo loader and transfer detection stored categories with no score,
    # which also reads as a person's. Rules would never re-file any of them.
    account = create_account(auth_client)["id"]
    coffee = _cat(auth_client, "Coffee")
    old_guess = _add(auth_client, account, "ZEBRA CAFE 1")
    edited_since = _add(auth_client, account, "ZEBRA CAFE 2")
    person = _add(auth_client, account, "ZEBRA CAFE 3")
    unscored_import = _add(auth_client, account, "ZEBRA CAFE 4")
    added_by_hand = _add(auth_client, account, "ZEBRA CAFE 5", category_id=coffee)
    locked = _add(auth_client, account, "ZEBRA CAFE 6")
    with SessionLocal() as db:
        rows = {t.id: t for t in db.execute(select(models.Transaction)).scalars()}
        made = rows[old_guess].created_at
        for txn_id, confidence, source, touched in (
            (old_guess, 1.0, "import", made),
            (edited_since, 1.0, "import", made + dt.timedelta(days=3)),
            (person, 1.0, "import", made + dt.timedelta(minutes=5)),
            (unscored_import, None, "import", made),
            (added_by_hand, None, "manual", made),
            (locked, 1.0, "import", made),
        ):
            t = rows[txn_id]
            t.category_id, t.confidence, t.source = coffee, confidence, source
            t.created_at = made
            t.updated_at = touched
        rows[locked].category_locked = True
        db.commit()

    _mark_automatic_categories(engine)
    _mark_automatic_categories(engine)  # and again: a second start changes nothing more

    assert _state(old_guess) == (coffee, ML_MAX_CONFIDENCE)
    assert _state(unscored_import) == (coffee, ML_MAX_CONFIDENCE)
    # Touched since import: a person may have chosen it, so it stays a person's.
    assert _state(edited_since) == (coffee, 1.0)
    assert _state(person) == (coffee, 1.0)
    assert _state(added_by_hand) == (coffee, None)
    assert _state(locked) == (coffee, 1.0)
    with SessionLocal() as db:
        # Bookkeeping, not a change anyone made: the row's "last changed" is left alone.
        marked = db.get(models.Transaction, old_guess)
        assert marked is not None and marked.updated_at == made
