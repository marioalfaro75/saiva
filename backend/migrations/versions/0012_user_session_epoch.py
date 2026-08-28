"""add user session_epoch

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-28 15:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stamped into session tokens so they can be invalidated without a session table.
    # Existing tokens carry no epoch and are read as 0, which matches this default, so
    # nobody is signed out by the upgrade itself.
    op.add_column(
        'users',
        sa.Column('session_epoch', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('users', 'session_epoch')
