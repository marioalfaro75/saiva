"""add import_profiles

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-24 13:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # How one shape of statement is read, keyed on a fingerprint of its header row, so
    # next month's export from the same bank opens already mapped.
    op.create_table(
        'import_profiles',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('household_id', sa.String(length=32), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('mapping', sa.JSON(), nullable=False),
        sa.Column('account_map', sa.JSON(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['households.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('household_id', 'fingerprint', name='uq_profile_shape'),
    )
    op.create_index(
        op.f('ix_import_profiles_household_id'), 'import_profiles', ['household_id']
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_import_profiles_household_id'), table_name='import_profiles')
    op.drop_table('import_profiles')
