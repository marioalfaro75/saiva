"""OFX files that carry more than one account.

A single download from a bank routinely covers several accounts, each in its own
STMTRS/CCSTMTRS aggregate. The parser used to scan every STMTTRN in the document
regardless of which statement it sat in, so those transactions were all filed under
whichever account the importer had been told to use — silently, and with no way to
tell afterwards.
"""

from app.services import importers

TWO_ACCOUNTS = b"""<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
  <CURDEF>AUD
  <BANKACCTFROM><BANKID>032000<ACCTID>111222333<ACCTTYPE>SAVINGS</BANKACCTFROM>
  <BANKTRANLIST>
    <STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20250701<TRNAMT>-42.50
      <FITID>A1<NAME>WOOLWORTHS 1234</STMTTRN>
    <STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20250702<TRNAMT>2500.00
      <FITID>A2<NAME>SALARY</STMTTRN>
  </BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
<CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
  <CURDEF>AUD
  <CCACCTFROM><ACCTID>4111111111115263</CCACCTFROM>
  <BANKTRANLIST>
    <STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20250703<TRNAMT>-19.90
      <FITID>C1<NAME>NETFLIX</STMTTRN>
  </BANKTRANLIST>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>"""

ONE_ACCOUNT = b"""<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
  <BANKACCTFROM><ACCTID>999888777</BANKACCTFROM>
  <BANKTRANLIST>
    <STMTTRN><DTPOSTED>20250705<TRNAMT>-10.00<FITID>S1<NAME>COFFEE</STMTTRN>
  </BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""

# No statement wrapper at all — the shape the parser used to assume.
BARE = b"""<OFX><BANKTRANLIST>
  <STMTTRN><DTPOSTED>20250706<TRNAMT>-5.00<FITID>B1<NAME>BUS FARE</STMTTRN>
</BANKTRANLIST></OFX>"""


def test_each_transaction_keeps_the_account_it_came_from() -> None:
    parsed = importers.parse_ofx(TWO_ACCOUNTS)
    assert len(parsed) == 3
    by_id = {p.provider_txn_id: p.account_value for p in parsed}
    assert by_id == {
        "A1": "111222333",
        "A2": "111222333",
        "C1": "4111111111115263",
    }


def test_a_transfers_own_acctid_is_not_mistaken_for_the_statements() -> None:
    """A transfer can name the other side of itself inside its STMTTRN. Reading the
    first ACCTID in the block would take that instead of the statement's."""
    ofx = b"""<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
      <BANKACCTFROM><ACCTID>111222333</BANKACCTFROM>
      <BANKTRANLIST>
        <STMTTRN><DTPOSTED>20250701<TRNAMT>-100.00<FITID>T1<NAME>TRANSFER
          <BANKACCTTO><ACCTID>555444333</BANKACCTTO></STMTTRN>
      </BANKTRANLIST>
    </STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    (txn,) = importers.parse_ofx(ofx)
    assert txn.account_value == "111222333"


def test_a_single_statement_still_reports_its_account() -> None:
    (txn,) = importers.parse_ofx(ONE_ACCOUNT)
    assert txn.account_value == "999888777"


def test_a_file_with_no_statement_wrapper_still_imports() -> None:
    """Unusual exports must keep working; they simply carry no account of their own,
    so the account chosen in the wizard applies as before."""
    (txn,) = importers.parse_ofx(BARE)
    assert txn.account_value is None
    assert txn.amount_cents == -500


def test_the_scan_lists_each_account_in_the_file() -> None:
    found = importers.scan_account_values(TWO_ACCOUNTS, "ofx", None)
    assert [(a.value, a.row_count) for a in found] == [
        ("111222333", 2),
        ("4111111111115263", 1),
    ]
    savings = found[0]
    assert savings.first_date.isoformat() == "2025-07-01"
    assert savings.last_date.isoformat() == "2025-07-02"
    assert savings.looks_mangled is False


def test_an_unclosed_acctfrom_still_yields_the_account() -> None:
    """OFX 1.x is SGML and some issuers leave the ACCTFROM aggregate unclosed, so the
    statement's account has to be read from what precedes the first transaction."""
    ofx = b"""<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
      <BANKACCTFROM>
      <BANKID>032000
      <ACCTID>777666555
      <ACCTTYPE>SAVINGS
      <BANKTRANLIST>
        <STMTTRN><DTPOSTED>20250701<TRNAMT>-12.00<FITID>U1<NAME>PARKING</STMTTRN>
      </BANKTRANLIST>
    </STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    (txn,) = importers.parse_ofx(ofx)
    assert txn.account_value == "777666555"
