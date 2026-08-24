"""Recognising which column is which in a statement.

Header names are the first evidence and the shape of the values is the second, so a
file whose columns are called something unexpected — or which has no header row —
is still read correctly. Both have to be able to say "I don't know" rather than
guess, because a wrong account column silently files transactions under the wrong
account.
"""

from app.services import importers


def col(body: bytes) -> tuple[int | None, str | None]:
    s = importers.sniff_csv(body)
    c = s.suggested_account_col
    return c, (s.columns[c] if c is not None else None)


def test_a_header_that_names_the_account_is_taken() -> None:
    body = (
        b"Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance\n"
        b"046568586577,14/08/2026,DEPOSIT,,1254.92,-819480.37\n"
        b"046568586577,13/08/2026,PAYMENT,42.50,,-820735.29\n"
    )
    assert col(body) == (0, "Bank Account")


def test_an_unexpected_header_is_caught_by_the_shape_of_the_values() -> None:
    """"Src" is in no keyword list; masked card numbers that repeat are unmistakable."""
    body = (
        b"Date,Src,Description,Amount\n"
        b"01/06/2025,****4417,UBER,-24.50\n"
        b"02/06/2025,****4417,NETFLIX,-19.90\n"
        b"03/06/2025,****9902,COLES,-30.00\n"
    )
    assert col(body) == (1, "Src")


def test_a_file_with_no_header_row_is_read_by_shape_alone() -> None:
    """With no headers the mapping falls back to positions, guessing column 1 is the
    description. That guess must not block detection of the column it guessed over."""
    body = (
        b"01/06/2025,062-000 12345678,WOOLWORTHS,-30.00\n"
        b"02/06/2025,062-000 12345678,BP,-70.00\n"
        b"03/06/2025,062-000 87654321,INTEREST,1.20\n"
        b"04/06/2025,062-000 87654321,FEE,-5.00\n"
    )
    assert col(body)[0] == 1


def test_a_reference_number_is_not_an_account() -> None:
    """One distinct value per row is a receipt number, however account-shaped."""
    body = (
        b"Date,Serial,Description,Amount\n"
        b"01/06/2025,000123,WOOLWORTHS,-30.00\n"
        b"02/06/2025,000124,BP,-70.00\n"
        b"03/06/2025,000125,INTEREST,1.20\n"
    )
    assert col(body) == (None, None)


def test_whole_dollar_amounts_are_not_mistaken_for_account_numbers() -> None:
    """A repeating column of 300000 looks exactly like an account number. What rules
    it out is that the header already claimed it as the amount."""
    body = (
        b"Date,Description,Amount\n"
        b"01/06/2025,WOOLWORTHS,300000\n"
        b"02/06/2025,BP,300000\n"
        b"03/06/2025,COLES,300000\n"
    )
    assert col(body) == (None, None)


def test_a_single_account_file_claims_nothing() -> None:
    body = b"Date,Description,Amount\n01/06/2025,WOOLWORTHS,-30.00\n02/06/2025,BP,-70.00\n"
    assert col(body) == (None, None)


def test_debit_amount_is_not_also_read_as_the_signed_amount_column() -> None:
    """"Debit Amount" contains "amount". Left unguarded, switching the file to a
    single signed column pre-selects the debit column and every credit becomes a
    debit."""
    body = (
        b"Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance\n"
        b"046568586577,14/08/2026,DEPOSIT,,1254.92,-819480.37\n"
    )
    m = importers.sniff_csv(body).suggested_mapping
    assert m.amount_mode == "debit_credit"
    assert (m.debit_col, m.credit_col) == (3, 4)
    assert m.amount_col is None
