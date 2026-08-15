"""allow an import batch to span multiple accounts

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-15 12:45:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A file whose rows carry an account column produces one batch covering several
    # accounts, so the batch itself no longer names one. Each transaction keeps its
    # own account_id, which is unchanged and still required.
    with op.batch_alter_table('import_batches') as batch:
        batch.alter_column(
            'account_id', existing_type=sa.String(length=32), nullable=True
        )


def downgrade() -> None:
    # Multi-account batches have no single account to fall back to; drop them so the
    # column can be made non-nullable again. Their transactions are left untouched.
    op.execute(sa.text('DELETE FROM import_batches WHERE account_id IS NULL'))
    with op.batch_alter_table('import_batches') as batch:
        batch.alter_column(
            'account_id', existing_type=sa.String(length=32), nullable=False
        )
