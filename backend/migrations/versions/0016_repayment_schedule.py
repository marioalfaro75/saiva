"""a home loan's regular repayment: amount and how often, from when

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-15 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The household's own regular repayment for a loan, as a list of periods:
    # [{"since": "2026-01-01" | null, "cents": 125492, "every": "weekly"}]. A repayment
    # near the amount in force on its date, once per period, is regular; the rest are
    # extra. NULL = not set: every repayment is regular, as before.
    op.add_column('accounts', sa.Column('repayment_schedule', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('accounts', 'repayment_schedule')
