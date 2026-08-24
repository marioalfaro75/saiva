"""add account bank_identifier

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-24 12:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The bank's own identifier for an account (statement account number, OFX ACCTID),
    # so a multi-account file maps itself on later imports. Nullable: accounts created
    # by hand have none, and matching falls back to the name.
    op.add_column(
        'accounts',
        sa.Column('bank_identifier', sa.String(length=64), nullable=True),
    )
    op.create_index('ix_accounts_bank_identifier', 'accounts', ['bank_identifier'])


def downgrade() -> None:
    op.drop_index('ix_accounts_bank_identifier', table_name='accounts')
    op.drop_column('accounts', 'bank_identifier')
