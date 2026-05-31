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
MATCH_TYPES = {"contains", "starts_with", "regex", "merchant"}

# Roles that may perform destructive/admin actions and write data.
ROLE_RANK = {"viewer": 0, "adult": 1, "owner": 2}

UNCATEGORISED = "Uncategorised"
TRANSFER_CATEGORY = "Internal transfers"
