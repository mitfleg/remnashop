from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Self
from uuid import UUID

from remnapy.enums.users import TrafficLimitStrategy

from src.core.enums import Currency, PlanAvailability, PlanType
from src.core.exceptions import PriceNotFoundError

from .base import BaseDto, TimestampMixin, TrackableMixin


@dataclass(kw_only=True)
class PlanSnapshotDto:
    id: int

    name: str
    tag: Optional[str] = None

    type: PlanType
    traffic_limit_strategy: TrafficLimitStrategy = TrafficLimitStrategy.NO_RESET

    traffic_limit: int
    device_limit: int
    duration: int

    internal_squads: list[UUID] = field(default_factory=list)
    external_squad: Optional[UUID] = None

    is_trial: bool = False

    @classmethod
    def from_plan(
        cls,
        plan: "PlanDto",
        duration: int,
        device_limit: Optional[int] = None,
    ) -> Self:
        return cls(
            id=plan.id,
            name=plan.name,
            tag=plan.tag,
            type=plan.type,
            traffic_limit_strategy=plan.traffic_limit_strategy,
            traffic_limit=plan.traffic_limit,
            device_limit=plan.resolve_device_limit(device_limit),
            duration=duration,
            internal_squads=plan.internal_squads,
            external_squad=plan.external_squad,
            is_trial=plan.is_trial,
        )

    @classmethod
    def test(cls) -> "PlanSnapshotDto":
        return cls(
            id=-1,
            name="test",
            tag=None,
            type=PlanType.UNLIMITED,
            traffic_limit=0,
            device_limit=0,
            duration=0,
            traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
            internal_squads=[],
            external_squad=None,
        )


@dataclass(kw_only=True)
class PlanDto(BaseDto, TrackableMixin, TimestampMixin):
    public_code: Optional[str] = None
    name: str = ""
    description: Optional[str] = None
    tag: Optional[str] = None

    type: PlanType = PlanType.BOTH
    availability: PlanAvailability = PlanAvailability.ALL
    traffic_limit_strategy: TrafficLimitStrategy = TrafficLimitStrategy.NO_RESET

    traffic_limit: int = 100
    device_limit: int = 1
    max_device_limit: int = 0

    allowed_telegram_ids: list[int] = field(default_factory=list)
    allowed_emails: list[str] = field(default_factory=list)
    internal_squads: list[UUID] = field(default_factory=list)
    external_squad: Optional[UUID] = None

    order_index: int = 0
    is_active: bool = False
    is_trial: bool = False

    durations: list["PlanDurationDto"] = field(default_factory=list)

    @property
    def is_unlimited_traffic(self) -> bool:
        return self.type not in {PlanType.TRAFFIC, PlanType.BOTH}

    @property
    def is_unlimited_devices(self) -> bool:
        return self.type not in {PlanType.DEVICES, PlanType.BOTH}

    @property
    def has_device_selection(self) -> bool:
        return not self.is_unlimited_devices and self.max_device_limit > self.device_limit

    def resolve_device_limit(self, requested: Optional[int] = None) -> int:
        if self.is_unlimited_devices:
            return 0
        if not self.has_device_selection:
            return self.device_limit

        selected = self.device_limit if requested is None else requested
        if selected < self.device_limit or selected > self.max_device_limit:
            raise ValueError(
                f"Device limit must be between {self.device_limit} and {self.max_device_limit}"
            )
        return selected

    def get_duration(self, days: int) -> Optional["PlanDurationDto"]:
        return next((d for d in self.durations if d.days == days), None)


@dataclass(kw_only=True)
class PlanDurationDto(BaseDto, TrackableMixin):
    days: int
    order_index: int = 0
    prices: list["PlanPriceDto"] = field(default_factory=list)

    def get_price(self, currency: Currency) -> Decimal:
        price = next((p.price for p in self.prices if p.currency == currency), None)
        if price is None:
            raise PriceNotFoundError(
                f"No price for currency '{currency}' in duration '{self.days}'"
            )
        return price

    def get_extra_device_price(self, currency: Currency) -> Decimal:
        price = next(
            (p.extra_device_price for p in self.prices if p.currency == currency),
            None,
        )
        if price is None:
            raise PriceNotFoundError(
                f"No extra device price for currency '{currency}' in duration '{self.days}'"
            )
        return price

    def get_price_for_devices(
        self,
        currency: Currency,
        base_device_limit: int,
        selected_device_limit: int,
    ) -> Decimal:
        extra_devices = max(0, selected_device_limit - base_device_limit)
        return self.get_price(currency) + self.get_extra_device_price(currency) * extra_devices


@dataclass(kw_only=True)
class PlanPriceDto(BaseDto, TrackableMixin):
    currency: Currency
    price: Decimal
    extra_device_price: Decimal = Decimal(0)
