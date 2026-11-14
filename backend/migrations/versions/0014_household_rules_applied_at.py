"""add households.rules_applied_at

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-14 14:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # When the household's rules were last brought to bear on every transaction they may
    # change. Before 0.17.5 a rule only filled blanks, and only on "Apply now"; 0.17.5
    # made a rule take effect when it changes, so a rule made earlier never did. app/dbinit.py
    # applies each household's rules once and records it here, so it happens once.
    #
    # Filled in by dbinit, not here: a migration may add but never touch a row
    # (tests/test_migrations_expand_only.py), which is what keeps a rollback safe.
    op.add_column('households', sa.Column('rules_applied_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('households', 'rules_applied_at')
