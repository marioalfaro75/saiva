"""add transaction provider_txn_id

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15 10:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Bank-assigned transaction id (OFX/QFX FITID). Nullable: existing rows and CSV
    # imports have none, and dedup falls back to hash/fuzzy matching when it is absent.
    op.add_column(
        'transactions',
        sa.Column('provider_txn_id', sa.String(length=64), nullable=True),
    )
    op.create_index(
        'ix_txn_account_provider', 'transactions', ['account_id', 'provider_txn_id']
    )


def downgrade() -> None:
    op.drop_index('ix_txn_account_provider', table_name='transactions')
    op.drop_column('transactions', 'provider_txn_id')
