"""File import: CSV (guided column mapping) and OFX/QFX parsing, with robust AU
date/amount handling and a stable de-duplication hash (PRD R4–R8)."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from dataclasses import dataclass
from typing import Literal

from dateutil import parser as dateparser

from ..schemas import CsvMapping, ImportSniffOut
from .merchants import normalise_merchant

AU_DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"]


@dataclass
class ParsedTxn:
    txn_date: dt.date
    amount_cents: int
    raw_description: str
    merchant: str
    # Bank-assigned unique id when the file carries one (OFX/QFX FITID); None for CSV.
    provider_txn_id: str | None = None
    # Raw value of the account column when the file covers several accounts.
    account_value: str | None = None


def parse_date(value: str, fmt: str | None = None) -> dt.date:
    v = (value or "").strip()
    if fmt:
        return dt.datetime.strptime(v, fmt).date()
    for f in AU_DATE_FORMATS:
        try:
            return dt.datetime.strptime(v, f).date()
        except ValueError:
            continue
    return dateparser.parse(v, dayfirst=True).date()


def to_cents(value: str, decimal: str = ".") -> int:
    s = (value or "").strip().replace(" ", "")
    if not s:
        return 0
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "")
    s = s.replace(".", "").replace(",", ".") if decimal == "," else s.replace(",", "")
    if s.startswith("-"):
        negative = True
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    try:
        cents = round(float(s) * 100)
    except ValueError:
        return 0
    return -cents if negative else cents


def _cell_cents(row: list[str], col: int | None, decimal: str) -> int:
    if col is None or col >= len(row):
        return 0
    return to_cents(row[col], decimal)


def dedup_hash(account_id: str, date: dt.date, amount_cents: int, description: str) -> str:
    norm = re.sub(r"\s+", " ", (description or "").lower()).strip()
    payload = f"{account_id}|{date.isoformat()}|{amount_cents}|{norm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode(content: bytes) -> str:
    return content.decode("utf-8-sig", errors="replace")


def _dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def read_rows(content: bytes) -> list[list[str]]:
    text = _decode(content)
    reader = csv.reader(io.StringIO(text), _dialect(text[:4096]))
    return [row for row in reader if any((c or "").strip() for c in row)]


def _find(headers: list[str], keys: list[str]) -> int | None:
    for i, h in enumerate(headers):
        if any(k in h for k in keys):
            return i
    return None


def _suggest_mapping(headers: list[str], has_header: bool) -> CsvMapping:
    lower = [h.lower() for h in headers]
    date_col = _find(lower, ["date"])
    desc_col = _find(
        lower, ["description", "narrative", "details", "reference", "transaction", "payee"]
    )
    amount_col = _find(lower, ["amount", "value"])
    debit_col = _find(lower, ["debit", "withdrawal", "paid out", "money out"])
    credit_col = _find(lower, ["credit", "deposit", "paid in", "money in"])
    balance_col = _find(lower, ["balance"])

    mode: Literal["single", "debit_credit"] = (
        "debit_credit" if (debit_col is not None or credit_col is not None) else "single"
    )

    return CsvMapping(
        has_header=has_header,
        date_col=date_col if date_col is not None else 0,
        description_col=desc_col if desc_col is not None else (1 if len(headers) > 1 else 0),
        amount_mode=mode,
        amount_col=amount_col,
        debit_col=debit_col,
        credit_col=credit_col,
        balance_col=balance_col,
    )


ACCOUNT_HEADER_KEYS = ["account", "acct", "bsb", "card", "product", "source"]


def _suggest_account_col(headers: list[str], body: list[list[str]]) -> int | None:
    """Spot a column that names the account each row belongs to.

    Needs an account-ish header, and values that repeat: a column holding a distinct
    value for every row is a reference or receipt number, not an account. Also caps
    the count, since anything beyond a handful is not something to map by hand.
    Returns a hint only; multi-account import stays off until the user opts in, so a
    wrong guess costs nothing.
    """
    col = _find([h.lower() for h in headers], ACCOUNT_HEADER_KEYS)
    if col is None or not body:
        return None
    values = {r[col].strip() for r in body if col < len(r) and r[col].strip()}
    if not values or len(values) > 20:
        return None
    if len(values) == len(body) and len(body) > 2:
        return None
    return col


def sniff_csv(content: bytes) -> ImportSniffOut:
    rows = read_rows(content)
    if not rows:
        return ImportSniffOut(
            detected_format="csv",
            has_header=True,
            columns=[],
            sample_rows=[],
            suggested_mapping=None,
        )
    text = _decode(content)
    try:
        has_header = csv.Sniffer().has_header(text[:4096])
    except csv.Error:
        has_header = True

    if has_header:
        headers = rows[0]
        body = rows[1:]
    else:
        headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
        body = rows

    return ImportSniffOut(
        detected_format="csv",
        has_header=has_header,
        columns=headers,
        sample_rows=body[:5],
        suggested_mapping=_suggest_mapping(headers, has_header),
        suggested_account_col=_suggest_account_col(headers, body),
    )


def parse_csv(content: bytes, mapping: CsvMapping) -> list[ParsedTxn]:
    rows = read_rows(content)
    start = mapping.skip_rows + (1 if mapping.has_header else 0)
    out: list[ParsedTxn] = []
    for row in rows[start:]:
        try:
            date = parse_date(row[mapping.date_col], mapping.date_format)
            description = row[mapping.description_col].strip()
        except (IndexError, ValueError):
            continue

        if mapping.amount_mode == "debit_credit":
            debit = abs(_cell_cents(row, mapping.debit_col, mapping.decimal))
            credit = abs(_cell_cents(row, mapping.credit_col, mapping.decimal))
            amount = credit - debit
        else:
            col = mapping.amount_col if mapping.amount_col is not None else mapping.description_col
            amount = to_cents(row[col], mapping.decimal) if col < len(row) else 0
            if mapping.invert_amount:
                amount = -amount

        if amount == 0 and not description:
            continue
        account_value = None
        if mapping.account_col is not None and mapping.account_col < len(row):
            account_value = row[mapping.account_col].strip()
        out.append(
            ParsedTxn(
                date,
                amount,
                description,
                normalise_merchant(description),
                account_value=account_value,
            )
        )
    return out


def scan_account_values(content: bytes, mapping: CsvMapping) -> list[tuple[str, int, str | None]]:
    """Distinct values of the account column, most common first, each with its row
    count and a sample description to help the user recognise what it refers to."""
    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    for p in parse_csv(content, mapping):
        value = (p.account_value or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
        samples.setdefault(value, p.raw_description)
    ordered = sorted(counts, key=lambda v: (-counts[v], v))
    return [(v, counts[v], samples.get(v)) for v in ordered]


def parse_ofx(content: bytes) -> list[ParsedTxn]:
    text = _decode(content)
    out: list[ParsedTxn] = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.S | re.I):

        def tag(name: str, b: str = block) -> str:
            m = re.search(rf"<{name}>([^<\r\n]*)", b, re.I)
            return m.group(1).strip() if m else ""

        raw_date = tag("DTPOSTED")[:8]
        try:
            date = dt.datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        name = tag("NAME") or tag("PAYEE")
        memo = tag("MEMO")
        description = " ".join(p for p in (name, memo) if p).strip()
        amount = to_cents(tag("TRNAMT"))
        out.append(
            ParsedTxn(
                date,
                amount,
                description,
                normalise_merchant(description),
                provider_txn_id=tag("FITID") or None,
            )
        )
    return out


def parse_file(content: bytes, file_format: str, mapping: CsvMapping | None) -> list[ParsedTxn]:
    fmt = file_format.lower()
    if fmt in ("ofx", "qfx"):
        return parse_ofx(content)
    if mapping is None:
        raise ValueError("CSV import requires a column mapping")
    return parse_csv(content, mapping)
