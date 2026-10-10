from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseSql
from .timestamp import TimestampMixin


class Gift(BaseSql, TimestampMixin):
    __tablename__ = "gifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[UUID] = mapped_column(unique=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    recipient_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    recipient_name: Mapped[str]
    recipient_username: Mapped[Optional[str]]
    remna_uuid: Mapped[UUID] = mapped_column(unique=True)
    paid_at: Mapped[Optional[datetime]]
    expire_at: Mapped[Optional[datetime]]
    subscription_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("subscriptions.id", ondelete="SET NULL"), unique=True
    )
    issued_at: Mapped[Optional[datetime]]
    buyer_unreachable_at: Mapped[Optional[datetime]]
    buyer_notified_at: Mapped[Optional[datetime]]
    recipient_notified_at: Mapped[Optional[datetime]]
    recipient_unreachable_at: Mapped[Optional[datetime]]
    recipient_seen_at: Mapped[Optional[datetime]]
