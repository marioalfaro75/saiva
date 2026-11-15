"""mortgages: a home loan's setup, and accounts counted in net worth

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-15 14:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A home loan's details, as the household enters them on the Mortgage page: what was
    # borrowed and when, the term, the rate over time, and which accounts offset it. The
    # balance owing is kept on the loan's account (its opening balance is set so the
    # account reads what is owed), so net worth and the projections agree.
    op.create_table(
        'mortgages',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('household_id', sa.String(length=32), nullable=False),
        sa.Column('account_id', sa.String(length=32), nullable=False),
        sa.Column('original_cents', sa.Integer(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('term_months', sa.Integer(), nullable=True),
        # [{"since": "2026-01-01" | null, "percent": 6.24}]
        sa.Column('rates', sa.JSON(), nullable=True),
        sa.Column('offset_account_ids', sa.JSON(), nullable=True),
        sa.Column('balance_as_of', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['households.id']),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', name='uq_mortgage_account'),
    )
    op.create_index(op.f('ix_mortgages_household_id'), 'mortgages', ['household_id'], unique=False)
    # Whether an account's balance counts in net worth: the household says so once its
    # balance is right (the loan and offsets set up on the Mortgage page, say).
    op.add_column(
        'accounts',
        sa.Column(
            'in_net_worth', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column('accounts', 'in_net_worth')
    op.drop_index(op.f('ix_mortgages_household_id'), table_name='mortgages')
    op.drop_table('mortgages')
