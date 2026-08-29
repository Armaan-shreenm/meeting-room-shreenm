"""Minimum party size per room, and no more role-based privilege.

Rooms are different sizes, so the booking form warns when a small group picks a
big room. ``min_people`` is that threshold, not a hard limit: the note is
advisory because nobody wants a booking refused over a headcount nobody checked.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Seeded here as well as in scripts/seed.py so an existing database gets the
# values without waiting for a deploy that happens to re-seed.
CAPACITIES = {
    "spark": 4,
    "power": 7,
    "pulse": 3,
    "ignite": 2,
    "switch": 2,
}


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("min_people", sa.Integer(), nullable=False, server_default="2"),
    )
    for room_id, minimum in CAPACITIES.items():
        op.execute(
            f"UPDATE rooms SET min_people = {minimum} WHERE id = '{room_id}'"
        )


def downgrade() -> None:
    op.drop_column("rooms", "min_people")
