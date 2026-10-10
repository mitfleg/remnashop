from typing import Any, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus

from .base import BaseSql
from .timestamp import TimestampMixin
from .user import User


class Transaction(BaseSql, TimestampMixin):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("gift_id", name="uq_transactions_gift_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[UUID] = mapped_column(index=True, unique=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )

    status: Mapped[TransactionStatus] = mapped_column(index=True)
    is_test: Mapped[bool]

    purchase_type: Mapped[PurchaseType]
    gateway_type: Mapped[PaymentGatewayType]
    gateway_display_name: Mapped[Optional[str]]
    payment_method: Mapped[Optional[str]]
    gift_id: Mapped[Optional[int]] = mapped_column(ForeignKey("gifts.id"))
    confirmation_url: Mapped[Optional[str]]

    pricing: Mapped[dict[str, Any]]
    currency: Mapped[Currency]
    plan_snapshot: Mapped[dict[str, Any]]

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
