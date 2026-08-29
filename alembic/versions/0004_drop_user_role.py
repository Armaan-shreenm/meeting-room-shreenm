"""Drop users.role and the user_role type.

Privilege levels were removed from the application first; this removes them from
the schema. Everybody is an ordinary user: anyone may book, and only the person
who booked something may cancel it.

Reinstating a role later is a fresh column and a fresh enum, which is a smaller
job than keeping a column nothing reads and everybody has to reason about.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("users", "role")
    op.execute("DROP TYPE IF EXISTS user_role;")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
                CREATE TYPE user_role AS ENUM ('EMPLOYEE', 'RECEPTION', 'ADMIN');
            END IF;
        END
        $$;
        """
    )
    op.add_column(
        "users",
        sa.Column(
            "role",
            postgresql.ENUM(
                "EMPLOYEE", "RECEPTION", "ADMIN", name="user_role", create_type=False
            ),
            nullable=False,
            server_default="EMPLOYEE",
        ),
    )
