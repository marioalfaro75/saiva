"""Allowed enumerated values, kept as plain sets for portable validation."""

ACCOUNT_TYPES = {
    "everyday",
    "savings",
    "credit_card",
    "home_loan",
    "offset",
    "personal_loan",
    "cash",
    "investment",
}
ROLES = {"owner", "adult", "viewer"}
CATEGORY_KINDS = {"income", "expense", "transfer", "savings"}
PERIOD_BASES = {"calendar", "weekly", "fortnightly", "monthly"}
BUDGET_PERIODS = {"monthly", "fortnightly", "annual"}
NET_WORTH_KINDS = {"asset", "liability"}
MATCH_TYPES = {"contains", "starts_with", "regex", "merchant", "equals"}

# Roles that may perform destructive/admin actions and write data.
ROLE_RANK = {"viewer": 0, "adult": 1, "owner": 2}

UNCATEGORISED = "Uncategorised"
TRANSFER_CATEGORY = "Internal transfers"
# Built-in categories the app itself files transactions into, so the household may
# rename them but not remove them: transfer detection needs somewhere to put transfers,
# and the categoriser somewhere to put what it cannot place. Matched on system_key.
PROTECTED_CATEGORY_KEYS = frozenset({UNCATEGORISED, TRANSFER_CATEGORY})
