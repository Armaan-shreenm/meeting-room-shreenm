"""Password hash and last login for directory users.

Phase 5 replaces the X-User-Email header with a real session. Passwords are
bcrypt hashes; the column is nullable so a directory member who has never had one
set simply cannot sign in, rather than the migration needing a placeholder.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "password_hash")
