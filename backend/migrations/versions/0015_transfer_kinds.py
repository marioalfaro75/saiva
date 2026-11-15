"""transfer kinds: what a linked pair is, wording per account, dismissed pairs

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-15 10:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # What a linked pair of transactions is -- a card payment, money moved between the
    # household's own accounts, or a loan repayment -- on both legs. NULL = not linked.
    op.add_column('transactions', sa.Column('transfer_kind', sa.String(length=16), nullable=True))
    # Phrases that, in another account's description, mean money is going to or coming
    # from this account ("By Authority To Cc"). Set by the household, or learned from a
    # suggestion they accepted. NULL = none yet.
    op.add_column('accounts', sa.Column('transfer_wording', sa.JSON(), nullable=True))
    # When dbinit last classified this household's transfers by kind: once, for history
    # from before kinds existed. Filled in by app/dbinit.py, never here.
    op.add_column(
        'households', sa.Column('transfers_classified_at', sa.DateTime(), nullable=True)
    )
    # Pairs a person said are not one transfer, so they are never suggested or linked again.
    op.create_table(
        'transfer_dismissals',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('household_id', sa.String(length=32), nullable=False),
        sa.Column('outflow_id', sa.String(length=32), nullable=False),
        sa.Column('inflow_id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['households.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('outflow_id', 'inflow_id', name='uq_transfer_dismissal_pair'),
    )
    op.create_index(
        op.f('ix_transfer_dismissals_household_id'),
        'transfer_dismissals',
        ['household_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_transfer_dismissals_household_id'), table_name='transfer_dismissals')
    op.drop_table('transfer_dismissals')
    op.drop_column('households', 'transfers_classified_at')
    op.drop_column('accounts', 'transfer_wording')
    op.drop_column('transactions', 'transfer_kind')
