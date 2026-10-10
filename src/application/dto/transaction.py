from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Self
from uuid import UUID

from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus

from .base import BaseDto, TimestampMixin, TrackableMixin
from .plan import PlanSnapshotDto


@dataclass(kw_only=True)
class PriceDetailsDto(TrackableMixin):
    original_amount: Decimal
    discount_percent: int
    final_amount: Decimal

    @property
    def is_free(self) -> bool:
        return self.final_amount == 0

    @classmethod
    def test(cls) -> Self:
        return cls(
            original_amount=Decimal(2),
            discount_percent=0,
            final_amount=Decimal(2),
        )


@dataclass(kw_only=True)
class TransactionDto(BaseDto, TrackableMixin, TimestampMixin):
    payment_id: UUID
    user_id: int

    status: TransactionStatus
    is_test: bool = False

    purchase_type: PurchaseType
    gateway_type: PaymentGatewayType
    gateway_display_name: Optional[str] = None
    payment_method: Optional[str] = None
    gift_id: Optional[int] = None
    confirmation_url: Optional[str] = None

    pricing: "PriceDetailsDto"
    currency: Currency
    plan_snapshot: "PlanSnapshotDto"

    @property
    def is_completed(self) -> bool:
        return self.status == TransactionStatus.COMPLETED

    @property
    def is_terminal(self) -> bool:
        return self.status in (TransactionStatus.COMPLETED, TransactionStatus.CANCELED)
