"""add transaction category_locked

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-06 13:58:21.611417
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills existing rows to False; the ORM sets the value going forward.
    op.add_column(
        'transactions',
        sa.Column('category_locked', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('transactions', 'category_locked')
