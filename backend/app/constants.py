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
CARD_PAYMENT_CATEGORY = "Credit-card payments"
CASH_DEPOSIT_CATEGORY = "Cash deposit"
MORTGAGE_CATEGORY = "Mortgage"
# A home loan repayment beyond the usual one. Built-in households before 0.18.1 called it
# "Offset/Extra repayments"; start-up renames that one (see dbinit).
EXTRA_REPAYMENT_CATEGORY = "Extra repayments"
OLD_EXTRA_REPAYMENT_CATEGORY = "Offset/Extra repayments"
LOAN_REPAYMENT_CATEGORY = "Loan repayments"

# What a linked pair of transactions is. Decided by the account the money went into.
CARD_PAYMENT = "card_payment"  # into a credit card: the card's purchases are the spending
OWN_ACCOUNTS = "own_accounts"  # between everyday, savings, offset and the like: not counted
LOAN_REPAYMENT = "loan_repayment"  # into a loan: the paying side counts as spending
TRANSFER_KINDS = (CARD_PAYMENT, OWN_ACCOUNTS, LOAN_REPAYMENT)

LOAN_ACCOUNT_TYPES = frozenset({"home_loan", "personal_loan"})
# Where money is spendable: what the forecast's balance and low-balance alerts add up.
# Cards and loans are debts, and adding a mortgage to a balance drags it far below zero.
CASH_ACCOUNT_TYPES = frozenset({"everyday", "savings", "offset", "cash"})
# Where money arriving from nowhere else the household holds is a cash deposit.
DEPOSIT_ACCOUNT_TYPES = frozenset({"savings", "offset"})
# Built-in categories the app itself files transactions into, so the household may
# rename them but not remove them: transfer detection needs somewhere to put transfers,
# and the categoriser somewhere to put what it cannot place. Matched on system_key.
PROTECTED_CATEGORY_KEYS = frozenset({UNCATEGORISED, TRANSFER_CATEGORY})
