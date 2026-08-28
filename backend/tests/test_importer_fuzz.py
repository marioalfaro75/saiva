"""Property-based fuzzing of the statement parsers.

These functions are the app's only untrusted input surface that does real work:
a file from the internet, parsed before anyone has looked at it. The contract they
have to hold is narrow — either produce rows, or raise something the API layer
already turns into a 400. Anything else is a 500 at best, and at worst the parser
believing something the file said.

Two of the bugs this suite was written to pin were found by hand first (`1e400`
crashing `to_cents`, a year-9999 date reaching the database). Fuzzing is here to
find the next one.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.schemas import CsvMapping
from app.services import importers

# Enough examples to be worth running, few enough to keep CI honest about its time.
FUZZ = settings(
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# What the API layer already handles. Anything outside this list reaches the user
# as a 500 and tells them nothing.
EXPECTED = (ValueError, UnicodeDecodeError, OverflowError, KeyError, IndexError)

cells = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=40,
)


@FUZZ
@given(cells)
def test_to_cents_always_returns_a_storable_integer(value: str) -> None:
    """It must never raise, and never return something the column cannot hold.

    The contract is deliberately total: `to_cents` is called per cell, and one bad
    cell in a 40,000-row file should not lose the other 39,999.
    """
    cents = importers.to_cents(value)
    assert isinstance(cents, int)
    assert abs(cents) <= importers.MAX_AMOUNT_CENTS


@FUZZ
@given(cells, st.sampled_from([".", ","]))
def test_to_cents_handles_either_decimal_convention(value: str, decimal: str) -> None:
    cents = importers.to_cents(value, decimal)
    assert abs(cents) <= importers.MAX_AMOUNT_CENTS


@FUZZ
@given(cells, st.booleans())
def test_parse_date_either_parses_or_raises_a_value_error(value: str, dayfirst: bool) -> None:
    """dateutil raises several types of its own; they all have to land as ValueError.

    An OverflowError or a TypeError out of here is a 500 on an upload.
    """
    try:
        parsed = importers.parse_date(value, dayfirst=dayfirst)
    except (ValueError, OverflowError, TypeError):
        return
    assert isinstance(parsed, dt.date)
    assert importers.MIN_TXN_DATE <= parsed <= importers.MAX_TXN_DATE


@FUZZ
@given(st.binary(min_size=0, max_size=800))
def test_sniffing_arbitrary_bytes_never_crashes_unexpectedly(content: bytes) -> None:
    """Whatever someone uploads, the first look at it has to survive."""
    try:
        out = importers.sniff_csv(content)
    except EXPECTED:
        return
    assert isinstance(out.columns, list)
    assert out.suggested_mapping is None or isinstance(out.suggested_mapping, CsvMapping)


@FUZZ
@given(st.binary(min_size=0, max_size=800))
def test_detect_delimiter_always_returns_one_character(content: bytes) -> None:
    try:
        delimiter = importers.detect_delimiter(content)
    except EXPECTED:
        return
    assert isinstance(delimiter, str) and len(delimiter) == 1


rows = st.lists(st.lists(cells, min_size=1, max_size=8), min_size=1, max_size=12)


@FUZZ
@given(rows, st.booleans())
def test_parsing_a_csv_with_a_suggested_mapping_holds_its_contract(
    table: list[list[str]], has_header: bool
) -> None:
    """The mapping the app suggests for a file must work on that same file.

    Suggesting a column index the rows do not have is how a parser reads the wrong
    field — quietly, with plausible-looking output.
    """
    width = len(table[0])
    assume(all(len(r) == width for r in table))
    content = "\n".join(",".join(c.replace(",", " ").replace("\n", " ") for c in r) for r in table)
    body = content.encode()

    try:
        sniffed = importers.sniff_csv(body)
    except EXPECTED:
        return
    mapping = sniffed.suggested_mapping
    assume(mapping is not None)
    assert mapping is not None

    try:
        parsed = importers.parse_csv(body, mapping)
    except EXPECTED:
        return

    for txn in parsed:
        assert importers.MIN_TXN_DATE <= txn.txn_date <= importers.MAX_TXN_DATE
        assert abs(txn.amount_cents) <= importers.MAX_AMOUNT_CENTS
        assert isinstance(txn.raw_description, str)


@FUZZ
@given(st.binary(min_size=0, max_size=1200))
def test_ofx_parsing_survives_arbitrary_bytes(content: bytes) -> None:
    """The OFX reader is a hand-written tag scanner, which is the risky kind."""
    try:
        parsed = importers.parse_ofx(content)
    except EXPECTED:
        return
    for txn in parsed:
        assert importers.MIN_TXN_DATE <= txn.txn_date <= importers.MAX_TXN_DATE
        assert abs(txn.amount_cents) <= importers.MAX_AMOUNT_CENTS


@FUZZ
@given(
    st.text(min_size=0, max_size=200),
    st.text(alphabet="<>/ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=12),
)
def test_the_tag_scanner_terminates_on_unbalanced_markup(text: str, tag: str) -> None:
    """It replaced a `<TAG>(.*?)</TAG>` regex that a crafted file could stall.

    Unbalanced or nested markup has to end the scan, not loop.
    """
    blocks = importers._blocks(text, f"<{tag}>", f"</{tag}>")
    assert isinstance(blocks, list)
    assert all(isinstance(b, str) for b in blocks)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\x00" * 100,
        b"\xff\xfe\x00\x00",
        b"Date,Amount\n" + b"9" * 400 + b",1.00\n",
        b"Date,Amount\n2026-01-01,1e400\n",
        b"Date,Amount\n9999-12-31,1.00\n",
        b"<OFXTRANSACTION><OFXTRANSACTION><OFXTRANSACTION>",
    ],
    ids=[
        "empty", "nul-bytes", "bad-utf16-bom", "very-long-cell",
        "float-overflow", "far-future-date", "unclosed-ofx-tags",
    ],
)
def test_the_shapes_that_actually_broke_it(content: bytes) -> None:
    """A fixed corpus alongside the generated one, so past bugs stay pinned."""
    for parse in (importers.sniff_csv, importers.parse_ofx):
        with contextlib.suppress(EXPECTED):
            parse(content)  # type: ignore[operator]
