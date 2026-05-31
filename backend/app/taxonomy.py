"""Default AU home-budget category taxonomy (PRD Appendix A) and starter rules.

Deliberately small and meaningful; users can edit. Each entry maps a parent
category (with a `kind`) to its example subcategories.
"""

DEFAULT_TAXONOMY: list[dict] = [
    {"name": "Income", "kind": "income", "children": [
        "Salary/Wages", "Government benefits", "Investment/Interest", "Refunds", "Other income"]},
    {"name": "Housing", "kind": "expense", "children": [
        "Mortgage", "Rent", "Offset/Extra repayments", "Strata/Body corporate",
        "Council rates", "Home insurance", "Repairs & maintenance"]},
    {"name": "Utilities", "kind": "expense", "children": [
        "Electricity", "Gas", "Water", "Internet", "Mobile phone"]},
    {"name": "Groceries", "kind": "expense", "children": ["Supermarkets", "Butcher/Greengrocer"]},
    {"name": "Transport", "kind": "expense", "children": [
        "Fuel", "Public transport", "Tolls", "Parking", "Rego & CTP",
        "Car insurance", "Servicing/Repairs", "Rideshare/Taxi"]},
    {"name": "Health", "kind": "expense", "children": [
        "Private health insurance", "Medicare/GP/Specialist", "Pharmacy", "Dental", "Optical"]},
    {"name": "Children & Education", "kind": "expense", "children": [
        "Childcare", "School fees", "School supplies", "Activities/Sport", "Tutoring"]},
    {"name": "Food & Drink (out)", "kind": "expense", "children": [
        "Restaurants/Cafés", "Takeaway", "Alcohol/Bars", "Coffee"]},
    {"name": "Shopping", "kind": "expense", "children": [
        "Clothing", "Homewares", "Electronics", "Gifts", "General merchandise"]},
    {"name": "Subscriptions & Memberships", "kind": "expense", "children": [
        "Streaming", "Software/Apps", "Gym", "News/Media", "Cloud storage"]},
    {"name": "Insurance & Finance", "kind": "expense", "children": [
        "Life/Income protection", "Bank fees", "Interest charges", "BNPL", "Loan repayments"]},
    {"name": "Personal care", "kind": "expense", "children": ["Hair/Beauty", "Cosmetics"]},
    {"name": "Entertainment & Recreation", "kind": "expense", "children": [
        "Events/Movies", "Hobbies", "Sport", "Books/Games"]},
    {"name": "Travel & Holidays", "kind": "expense", "children": [
        "Flights", "Accommodation", "Holiday spending"]},
    {"name": "Pets", "kind": "expense", "children": ["Pet food", "Vet", "Pet other"]},
    {"name": "Donations & Gifts", "kind": "expense",
     "children": ["Charity/Donations", "Gifts given"]},
    {"name": "Taxes & Government", "kind": "expense", "children": [
        "ATO/Tax", "Fines/Penalties", "HECS/HELP"]},
    {"name": "Savings & Investments", "kind": "savings", "children": [
        "Transfers to savings", "Super contributions (voluntary)", "Investments"]},
    {"name": "Transfers", "kind": "transfer", "children": [
        "Internal transfers", "Credit-card payments"]},
    {"name": "Uncategorised", "kind": "expense", "children": []},
]

# Starter system rules: (match_type, pattern, target subcategory name).
# Patterns are lower-cased substrings unless match_type says otherwise.
STARTER_RULES: list[tuple[str, str, str]] = [
    ("contains", "woolworths", "Supermarkets"),
    ("contains", "coles", "Supermarkets"),
    ("contains", "aldi", "Supermarkets"),
    ("contains", "iga", "Supermarkets"),
    ("contains", "costco", "Supermarkets"),
    ("contains", "bp ", "Fuel"),
    ("contains", "caltex", "Fuel"),
    ("contains", "ampol", "Fuel"),
    ("contains", "shell", "Fuel"),
    ("contains", "7-eleven", "Fuel"),
    ("contains", "uber trip", "Rideshare/Taxi"),
    ("contains", "didi", "Rideshare/Taxi"),
    ("contains", "opal", "Public transport"),
    ("contains", "myki", "Public transport"),
    ("contains", "linkt", "Tolls"),
    ("contains", "uber eats", "Takeaway"),
    ("contains", "menulog", "Takeaway"),
    ("contains", "doordash", "Takeaway"),
    ("contains", "mcdonald", "Takeaway"),
    ("contains", "kfc", "Takeaway"),
    ("contains", "netflix", "Streaming"),
    ("contains", "spotify", "Streaming"),
    ("contains", "disney", "Streaming"),
    ("contains", "youtube premium", "Streaming"),
    ("contains", "amazon prime", "Streaming"),
    ("contains", "apple.com/bill", "Software/Apps"),
    ("contains", "google", "Software/Apps"),
    ("contains", "anytime fitness", "Gym"),
    ("contains", "fitness first", "Gym"),
    ("contains", "afterpay", "BNPL"),
    ("contains", "zip pay", "BNPL"),
    ("contains", "humm", "BNPL"),
    ("contains", "agl", "Electricity"),
    ("contains", "origin energy", "Electricity"),
    ("contains", "energy australia", "Electricity"),
    ("contains", "telstra", "Mobile phone"),
    ("contains", "optus", "Mobile phone"),
    ("contains", "vodafone", "Mobile phone"),
    ("contains", "tpg", "Internet"),
    ("contains", "aussie broadband", "Internet"),
    ("contains", "chemist warehouse", "Pharmacy"),
    ("contains", "priceline", "Pharmacy"),
    ("contains", "bunnings", "Homewares"),
    ("contains", "kmart", "General merchandise"),
    ("contains", "target", "General merchandise"),
    ("contains", "big w", "General merchandise"),
    ("contains", "jb hi-fi", "Electronics"),
    ("contains", "officeworks", "Electronics"),
    ("contains", "salary", "Salary/Wages"),
    ("contains", "payroll", "Salary/Wages"),
    ("contains", "centrelink", "Government benefits"),
    ("contains", "medicare benefit", "Medicare/GP/Specialist"),
    ("contains", "ato ", "ATO/Tax"),
    ("contains", "account fee", "Bank fees"),
    ("contains", "monthly fee", "Bank fees"),
    ("contains", "interest charged", "Interest charges"),
]


def flatten(taxonomy: list[dict] | None = None) -> list[tuple[str, str | None, str]]:
    """Return (name, parent_name_or_None, kind) tuples for seeding."""
    src = taxonomy if taxonomy is not None else DEFAULT_TAXONOMY
    rows: list[tuple[str, str | None, str]] = []
    for parent in src:
        rows.append((parent["name"], None, parent["kind"]))
        for child in parent["children"]:
            rows.append((child, parent["name"], parent["kind"]))
    return rows
