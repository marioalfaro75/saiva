"""A note worth offering for a transaction, from its description.

Payments to people and accounts the household doesn't track, and deposits into
savings, are where a note says what the money was for. Banks often already carry the
answer: who it went to ("OSKO PAYMENT TO JOHN SMITH") and the reference typed with it
("REF BIRTHDAY GIFT"). This finds those, so the note is one click; a person can always
write their own.
"""

from __future__ import annotations

import re

# "to John Smith", "from Everyday": who the money went to or came from.
_FLOW = re.compile(r"\b(to|from)\s+(.+)$", re.IGNORECASE)
# What follows a reference marker: what the person typed when they sent it.
_REFERENCE = re.compile(r"\s(?:ref(?:erence)?\.?:?|-|–|—)\s+(.+)$", re.IGNORECASE)


def _tidy(text: str) -> str:
    words = " ".join(re.sub(r"\d+", " ", text).split()).strip(" -–—/.,:")
    # Banks shout; a note shouldn't.
    return words.title() if words.isupper() else words


def suggest_note(description: str | None) -> str | None:
    text = " ".join((description or "").split())
    reference = None
    found = _REFERENCE.search(text)
    if found:
        reference = _tidy(found.group(1)) or None
        text = text[: found.start()]
    who = None
    flow = _FLOW.search(text)
    if flow:
        rest = _tidy(flow.group(2))
        if rest:
            who = f"{flow.group(1).capitalize()} {rest}"
    parts = [p for p in (who, reference) if p]
    return " · ".join(parts) if parts else None
