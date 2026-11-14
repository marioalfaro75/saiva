"""add categories.system_key

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-14 08:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The name a built-in category was created with, kept when the household renames it.
    # Behaviour that has to find one specific built-in category -- transfer detection
    # ("Internal transfers"), the categoriser's fallback and the advisor ("Uncategorised"),
    # the benchmarks (by group) -- looks it up by this, so renaming is only a label.
    #
    # Filled in by app/dbinit.py, not here: a migration may add but never touch a row
    # (tests/test_migrations_expand_only.py), which is what keeps a rollback safe.
    op.add_column('categories', sa.Column('system_key', sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column('categories', 'system_key')
