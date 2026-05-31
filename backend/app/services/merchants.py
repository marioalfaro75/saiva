"""Merchant normalisation: turn a messy bank description into a tidy name (PRD R10)."""

from __future__ import annotations

import re

# Known AU merchants, ordered most-specific first so "uber eats" beats "uber".
KNOWN_MERCHANTS: list[tuple[str, str]] = [
    ("uber eats", "Uber Eats"),
    ("woolworths", "Woolworths"),
    ("coles", "Coles"),
    ("aldi", "Aldi"),
    ("costco", "Costco"),
    ("iga", "IGA"),
    ("bunnings", "Bunnings"),
    ("officeworks", "Officeworks"),
    ("jb hi-fi", "JB Hi-Fi"),
    ("big w", "Big W"),
    ("kmart", "Kmart"),
    ("target", "Target"),
    ("chemist warehouse", "Chemist Warehouse"),
    ("priceline", "Priceline"),
    ("netflix", "Netflix"),
    ("spotify", "Spotify"),
    ("disney", "Disney+"),
    ("amazon prime", "Amazon Prime"),
    ("amazon", "Amazon"),
    ("mcdonald", "McDonald's"),
    ("kfc", "KFC"),
    ("menulog", "Menulog"),
    ("doordash", "DoorDash"),
    ("uber", "Uber"),
    ("didi", "DiDi"),
    ("ampol", "Ampol"),
    ("caltex", "Caltex"),
    ("shell", "Shell"),
    ("7-eleven", "7-Eleven"),
    ("telstra", "Telstra"),
    ("optus", "Optus"),
    ("vodafone", "Vodafone"),
    ("agl", "AGL"),
    ("origin energy", "Origin Energy"),
    ("energy australia", "EnergyAustralia"),
    ("afterpay", "Afterpay"),
    ("zip pay", "Zip"),
    ("paypal", "PayPal"),
]

_PREFIX = re.compile(
    r"^(eftpos|osko payment|osko|payid|bpay|direct debit|visa purchase|"
    r"debit card purchase|card purchase|purchase|withdrawal|deposit|payment|transfer)\b[\s\-:]*",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\b[\d*x]{3,}\b", re.IGNORECASE)
_NON_NAME = re.compile(r"[^a-z0-9&/\-' ]", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def normalise_merchant(raw: str | None) -> str:
    low = (raw or "").lower()
    for key, display in KNOWN_MERCHANTS:
        if key in low:
            return display

    s = _PREFIX.sub("", raw or "")
    s = _LONG_DIGITS.sub(" ", s)
    s = _NON_NAME.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    if not s:
        return (raw or "").strip()[:60]
    name = " ".join(s.split(" ")[:4])
    return name.title()
