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

DAY_FIRST_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"]
MONTH_FIRST_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d", "%b %d %Y", "%B %d %Y"]


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
    # Running balance where the file reports one. Not stored on the transaction; used
    # to tell one account value from another during import.
    balance_cents: int | None = None


def parse_date(value: str, fmt: str | None = None, dayfirst: bool = True) -> dt.date:
    """Read a date, day first unless told otherwise.

    Day first is right for Australian statements and wrong for American ones, and
    01/07/2025 is a valid date either way — so getting it wrong files a year of
    transactions into the wrong months without a single row failing to parse. It is
    settable rather than guessed for exactly that reason.
    """
    v = (value or "").strip()
    if fmt:
        return dt.datetime.strptime(v, fmt).date()
    for f in DAY_FIRST_FORMATS if dayfirst else MONTH_FIRST_FORMATS:
        try:
            return dt.datetime.strptime(v, f).date()
        except ValueError:
            continue
    return dateparser.parse(v, dayfirst=dayfirst).date()


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


DELIMITERS = ",;\t|"


def detect_delimiter(content: bytes) -> str:
    """The character separating the columns.

    Python's sniffer guesses from a sample and gets it wrong often enough to matter —
    a tab-separated export whose narratives contain commas can come back as
    comma-separated, and then the whole file reads as a single column. The guess is
    surfaced in the mapping step so it can be corrected, which is why this returns the
    character rather than a dialect.
    """
    text = _decode(content)[:4096]
    try:
        return csv.Sniffer().sniff(text, delimiters=DELIMITERS).delimiter
    except csv.Error:
        # Fall back to whichever candidate divides the first lines most consistently.
        lines = [ln for ln in text.splitlines()[:10] if ln.strip()]
        if not lines:
            return ","
        best, best_score = ",", 0
        for d in DELIMITERS:
            counts = [ln.count(d) for ln in lines]
            if counts[0] and len(set(counts)) == 1 and counts[0] > best_score:
                best, best_score = d, counts[0]
        return best


def read_rows(content: bytes, delimiter: str | None = None) -> list[list[str]]:
    text = _decode(content)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter or detect_delimiter(content))
    return [row for row in reader if any((c or "").strip() for c in row)]


def _find(headers: list[str], keys: list[str], exclude: set[int] | None = None) -> int | None:
    for i, h in enumerate(headers):
        if exclude and i in exclude:
            continue
        if any(k in h for k in keys):
            return i
    return None


DATE_KEYS = ["date"]
DESC_KEYS = ["description", "narrative", "details", "reference", "transaction", "payee"]
AMOUNT_KEYS = ["amount", "value"]
DEBIT_KEYS = ["debit", "withdrawal", "paid out", "money out"]
CREDIT_KEYS = ["credit", "deposit", "paid in", "money in"]
BALANCE_KEYS = ["balance"]


def _by_header(headers: list[str]) -> dict[str, int | None]:
    """Every column the headers actually name. Nothing here is a default: a caller
    that needs one applies it, and a caller deciding what is already spoken for can
    tell a match from a guess."""
    lower = [h.lower() for h in headers]
    debit = _find(lower, DEBIT_KEYS)
    credit = _find(lower, CREDIT_KEYS)
    return {
        "date": _find(lower, DATE_KEYS),
        "description": _find(lower, DESC_KEYS),
        "debit": debit,
        "credit": credit,
        "balance": _find(lower, BALANCE_KEYS),
        # Looked for after debit and credit, and never one of them: "Debit Amount"
        # contains "amount", so a plain search picks it as the single signed column.
        # That is unused while the file reads as debit/credit, but it is what the
        # Amount field pre-selects if the mode is switched — turning every credit into
        # a debit without a word.
        "amount": _find(
            lower, AMOUNT_KEYS, exclude={c for c in (debit, credit) if c is not None}
        ),
    }


def _suggest_mapping(headers: list[str], has_header: bool) -> CsvMapping:
    found = _by_header(headers)
    date_col = found["date"]
    desc_col = found["description"]
    debit_col = found["debit"]
    credit_col = found["credit"]
    balance_col = found["balance"]
    amount_col = found["amount"]

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


ACCOUNT_HEADER_KEYS = [
    "account", "acct", "bsb", "card", "product", "source", "wallet", "a/c", "acc no",
]

# What an account identifier looks like when the header does not say so: a BSB and
# number, a masked card, a plain account number, or one Excel has turned into a float.
ACCOUNT_VALUE = re.compile(
    r"""^(?:
        \d{3}-?\d{3}[\s-]?\d{4,10}      # BSB + account, spaced or hyphenated
      | [*xX]{2,}[\s-]?\d{3,4}          # masked card: ****4417, xxxx4417
      | \d{6,16}                        # plain account number
      | \d(?:\.\d+)?[eE][+-]?\d+        # Excel-mangled account number
    )$""",
    re.X,
)

# How many different accounts one file can name and still be worth mapping by hand.
MAX_ACCOUNT_VALUES = 50


def _column_values(col: int, body: list[list[str]]) -> list[str]:
    return [r[col].strip() for r in body if col < len(r) and r[col].strip()]


def _repeats_like_an_account(values: list[str], rows: int, *, limit: int) -> bool:
    """A column of accounts holds few values, each used many times. One distinct value
    per row is a reference or receipt number, not an account."""
    distinct = set(values)
    if not distinct or len(distinct) > limit:
        return False
    return not (len(distinct) == rows and rows > 2)


def _claimed(headers: list[str]) -> set[int]:
    """Columns a header actually names, so the shape fallback does not take one.

    Positional defaults are deliberately excluded. A headerless file has no evidence
    for anything, so the mapping guesses column 1 is the description — and a guess
    must not block detection of the very column it guessed over.
    """
    return {c for c in _by_header(headers).values() if c is not None}


def _suggest_account_col(
    headers: list[str], body: list[list[str]], claimed: set[int] | None = None
) -> int | None:
    """Spot the column naming the account each row belongs to.

    Tried by header first, then by the shape of the values, so a column called
    "Wallet" or a file with no header row is still recognised. The value test is the
    stricter of the two: with no name to corroborate it, it wants a repeating column
    of things that look like account identifiers, and it ignores columns the mapping
    has already claimed — a column of whole-dollar amounts is indistinguishable from
    a column of account numbers on shape alone.
    """
    if not body:
        return None
    col = _find([h.lower() for h in headers], ACCOUNT_HEADER_KEYS)
    if col is not None and _repeats_like_an_account(
        _column_values(col, body), len(body), limit=MAX_ACCOUNT_VALUES
    ):
        return col

    for i in range(len(headers)):
        if claimed and i in claimed:
            continue
        values = _column_values(i, body)
        if not values or not _repeats_like_an_account(values, len(body), limit=20):
            continue
        if sum(1 for v in values if ACCOUNT_VALUE.match(v)) >= len(values) * 0.9:
            return i
    return None


def sniff_csv(content: bytes) -> ImportSniffOut:
    delimiter = detect_delimiter(content)
    rows = read_rows(content, delimiter)
    if not rows:
        return ImportSniffOut(
            detected_format="csv",
            has_header=True,
            columns=[],
            sample_rows=[],
            suggested_mapping=None,
            delimiter=delimiter,
            fingerprint="",
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

    mapping = _suggest_mapping(headers, has_header)
    mapping.delimiter = delimiter
    return ImportSniffOut(
        detected_format="csv",
        has_header=has_header,
        columns=headers,
        sample_rows=body[:5],
        suggested_mapping=mapping,
        suggested_account_col=_suggest_account_col(headers, body, _claimed(headers)),
        delimiter=delimiter,
        fingerprint=fingerprint(headers, has_header),
    )


def parse_csv(content: bytes, mapping: CsvMapping) -> list[ParsedTxn]:
    rows = read_rows(content, mapping.delimiter)
    start = mapping.skip_rows + (1 if mapping.has_header else 0)
    out: list[ParsedTxn] = []
    for row in rows[start:]:
        try:
            date = parse_date(row[mapping.date_col], mapping.date_format, mapping.dayfirst)
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
        balance = (
            _cell_cents(row, mapping.balance_col, mapping.decimal)
            if mapping.balance_col is not None
            else None
        )
        out.append(
            ParsedTxn(
                date,
                amount,
                description,
                normalise_merchant(description),
                account_value=account_value,
                balance_cents=balance,
            )
        )
    return out


# Excel turns a long account number with no leading zero into a float, so
# 734364123456 comes back as "7.34364E+11" with only six significant figures. The
# value still identifies the account consistently within one file, but the original
# number is gone, so it must never be stored as an account's lasting identifier.
MANGLED_NUMBER = re.compile(r"^\d(?:\.\d+)?[eE][+-]?\d+$")


@dataclass
class AccountValue:
    """One distinct value of the account column, with enough about it to recognise
    which of your accounts it is. A bare number like "7.34364E+11" is unidentifiable
    on its own; a balance of -819,480.37 over 74 rows is obviously the mortgage."""

    value: str
    row_count: int
    sample_description: str | None
    first_date: dt.date | None
    last_date: dt.date | None
    latest_balance_cents: int | None
    looks_mangled: bool


def account_values(parsed: list[ParsedTxn]) -> list[AccountValue]:
    """Summarise each distinct account value, most rows first."""
    groups: dict[str, list[ParsedTxn]] = {}
    for p in parsed:
        value = (p.account_value or "").strip()
        if value:
            groups.setdefault(value, []).append(p)

    out: list[AccountValue] = []
    for value, txns in groups.items():
        dated = sorted(txns, key=lambda t: t.txn_date)
        with_balance = [t for t in dated if t.balance_cents is not None]
        out.append(
            AccountValue(
                value=value,
                row_count=len(txns),
                sample_description=next(
                    (t.raw_description for t in txns if t.raw_description), None
                ),
                first_date=dated[0].txn_date if dated else None,
                last_date=dated[-1].txn_date if dated else None,
                # The balance on the most recent row, which is the one worth showing.
                latest_balance_cents=with_balance[-1].balance_cents if with_balance else None,
                looks_mangled=bool(MANGLED_NUMBER.match(value)),
            )
        )
    out.sort(key=lambda a: (-a.row_count, a.value))
    return out


def fingerprint(columns: list[str], has_header: bool) -> str:
    """A stable name for this shape of file.

    Built from the header row, case- and space-insensitively so cosmetic changes do
    not orphan a saved mapping. A file with no header has no names to go on, so its
    shape is just the column count — weaker, and deliberately so: it will collide with
    any other headerless file of the same width, which is why the mapping step is
    always shown rather than silently applied.

    A bank adding or renaming a column changes the fingerprint, and that is the point.
    The profile stops matching, the step shows the new column, and you decide.
    """
    if has_header:
        parts = [re.sub(r"\s+", " ", c).strip().lower() for c in columns]
        payload = "h|" + "|".join(parts)
    else:
        payload = f"n|{len(columns)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def durable_identifier(value: str) -> str | None:
    """The value if it can be trusted to name the same account in a later file.

    Two kinds cannot. A number Excel has rendered in scientific notation has lost its
    digits, so a clean export later would not match it. A short fragment — a card's
    last four, say — is not unique enough to bind an account to, and a different card
    ending in the same digits would silently inherit it. Both still map fine for the
    file in hand; they just are not remembered.
    """
    v = (value or "").strip()
    if not v or MANGLED_NUMBER.match(v):
        return None
    return v if len(re.sub(r"\D", "", v)) >= 6 else None


def scan_account_values(
    content: bytes, file_format: str, mapping: CsvMapping | None
) -> list[AccountValue]:
    """Account values in a file, whatever the format. OFX carries them per statement;
    CSV carries them in a column the mapping names."""
    return account_values(parse_file(content, file_format, mapping))


_STATEMENT = re.compile(r"<(STMTRS|CCSTMTRS)>(.*?)</\1>", re.S | re.I)
_ACCTFROM = re.compile(r"<(?:BANK|CC)ACCTFROM>(.*?)</(?:BANK|CC)ACCTFROM>", re.S | re.I)
_TXN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S | re.I)


def _ofx_tag(name: str, block: str) -> str:
    """Read one OFX element. Values run to the next tag or line end because OFX 1.x is
    SGML and leaves elements unclosed; only aggregates carry a closing tag."""
    m = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def _statement_account(block: str) -> str | None:
    """The account a statement belongs to, from its ACCTFROM aggregate.

    Scoped to that aggregate rather than the whole block: a transfer's STMTTRN can
    carry its own ACCTID for the other side, which would otherwise be picked up.
    """
    m = _ACCTFROM.search(block)
    if m:
        return _ofx_tag("ACCTID", m.group(1)) or None
    # Some issuers leave ACCTFROM unclosed. Anything before the first transaction is
    # still statement-level, so look only there.
    head = block[: m.start()] if (m := _TXN.search(block)) else block
    return _ofx_tag("ACCTID", head) or None


def _statements(text: str) -> list[tuple[str | None, str]]:
    """(account id, block) for each statement in the document.

    OFX wraps every account's transactions in its own STMTRS/CCSTMTRS aggregate, so
    one download can carry several accounts. Reading STMTTRN across the whole
    document silently merges them into whichever account the importer was told to
    use — the transactions land under the wrong account with nothing to show for it.

    A document with no recognisable statement wrapper is treated as a single
    unattributed statement, so unusual exports still import as they always did.
    """
    blocks = [(_statement_account(body), body) for _kind, body in _STATEMENT.findall(text)]
    return blocks or [(None, text)]


def parse_ofx(content: bytes) -> list[ParsedTxn]:
    text = _decode(content)
    out: list[ParsedTxn] = []
    for account_value, statement in _statements(text):
        for block in _TXN.findall(statement):
            raw_date = _ofx_tag("DTPOSTED", block)[:8]
            try:
                date = dt.datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                continue
            name = _ofx_tag("NAME", block) or _ofx_tag("PAYEE", block)
            memo = _ofx_tag("MEMO", block)
            description = " ".join(p for p in (name, memo) if p).strip()
            out.append(
                ParsedTxn(
                    date,
                    to_cents(_ofx_tag("TRNAMT", block)),
                    description,
                    normalise_merchant(description),
                    provider_txn_id=_ofx_tag("FITID", block) or None,
                    account_value=account_value,
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
