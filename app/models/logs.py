"""Notification and audit trails - spec section 11."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import (
    NOTIFICATION_EVENT_ENUM,
    NOTIFICATION_STATUS_ENUM,
    NotificationEvent,
    NotificationStatus,
)

if TYPE_CHECKING:
    from app.models.booking import Booking


class NotificationLog(Base):
    """One row per message NM Meet attempted to send.

    Section 8 says notifications are always sent with no opt-out, so this table
    is the evidence that they were. A row is written QUEUED before any send is
    attempted, then moved to SENT or FAILED, so a crash mid-send leaves a record
    rather than silence.
    """

    __tablename__ = "notification_log"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The mail address written to. Recipients include the reception and branch
    # mailboxes, which are not directory users, so this is an address not an FK.
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    event: Mapped[NotificationEvent] = mapped_column(
        PgEnum(NotificationEvent, name=NOTIFICATION_EVENT_ENUM, create_type=False),
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[NotificationStatus] = mapped_column(
        PgEnum(NotificationStatus, name=NOTIFICATION_STATUS_ENUM, create_type=False),
        nullable=False,
        default=NotificationStatus.QUEUED,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    booking: Mapped["Booking"] = relationship(viewonly=True)

    def __repr__(self) -> str:
        return f"<NotificationLog {self.event} to {self.recipient}>"


class AuditLog(Base):
    """Immutable record of what changed, by whom.

    Section 9 point 6: a booking is kept for the audit record and never deleted.
    ``before`` and ``after`` hold JSONB snapshots so a change is readable without
    replaying the application. A no-show release writes a row here and sends no
    notification, so this is the only trace it leaves.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    # Nullable: a scheduled job acts with no human actor.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.entity}:{self.entity_id} {self.action}>"
