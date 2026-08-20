from decimal import Decimal

import pytest

from src.application.dto import PlanDto, PlanDurationDto, PlanPriceDto, PlanSnapshotDto, UserDto
from src.application.services import PricingService
from src.application.use_cases.plan.commands.edit import (
    UpdatePlanDevice,
    UpdatePlanDeviceDto,
    UpdatePlanPrice,
    UpdatePlanPriceDto,
)
from src.application.use_cases.plan.queries.match import MatchPlan
from src.core.enums import Currency, PlanType


def make_plan() -> PlanDto:
    return PlanDto(
        id=2,
        name="Premium",
        type=PlanType.DEVICES,
        traffic_limit=0,
        device_limit=4,
        max_device_limit=10,
        durations=[
            PlanDurationDto(
                days=30,
                prices=[
                    PlanPriceDto(
                        currency=Currency.RUB,
                        price=Decimal("700"),
                        extra_device_price=Decimal("100"),
                    ),
                    PlanPriceDto(
                        currency=Currency.USD,
                        price=Decimal("10"),
                        extra_device_price=Decimal("1.425"),
                    ),
                    PlanPriceDto(
                        currency=Currency.XTR,
                        price=Decimal("125"),
                        extra_device_price=Decimal("12.50"),
                    ),
                ],
            )
        ],
    )


def test_plan_snapshot_keeps_paid_device_limit() -> None:
    plan = make_plan()

    snapshot = PlanSnapshotDto.from_plan(plan, duration=30, device_limit=8)

    assert snapshot.device_limit == 8
    with pytest.raises(ValueError):
        PlanSnapshotDto.from_plan(plan, duration=30, device_limit=11)


def test_device_price_is_added_before_discount() -> None:
    plan = make_plan()
    user = UserDto(id=1, name="Test", personal_discount=10)
    duration = plan.get_duration(30)
    assert duration is not None

    price = PricingService().calculate_for_duration(
        user,
        duration,
        Currency.RUB,
        base_device_limit=plan.device_limit,
        selected_device_limit=8,
    )

    assert price.original_amount == Decimal("1100")
    assert price.discount_percent == 10
    assert price.final_amount == Decimal("990")


def test_fractional_xtr_unit_price_is_rounded_after_multiplication() -> None:
    plan = make_plan()
    user = UserDto(id=1, name="Test")
    duration = plan.get_duration(30)
    assert duration is not None

    price = PricingService().calculate_for_duration(
        user,
        duration,
        Currency.XTR,
        base_device_limit=plan.device_limit,
        selected_device_limit=8,
    )

    assert price.final_amount == Decimal("175")


def test_fractional_usd_unit_price_reaches_exact_variant_total() -> None:
    plan = make_plan()
    user = UserDto(id=1, name="Test")
    duration = plan.get_duration(30)
    assert duration is not None

    price = PricingService().calculate_for_duration(
        user,
        duration,
        Currency.USD,
        base_device_limit=plan.device_limit,
        selected_device_limit=8,
    )

    assert price.final_amount == Decimal("15.70")


@pytest.mark.asyncio
async def test_admin_can_configure_device_range() -> None:
    plan = make_plan()

    updated = await UpdatePlanDevice().system(UpdatePlanDeviceDto(plan, "5-12"))

    assert updated.device_limit == 5
    assert updated.max_device_limit == 12


@pytest.mark.asyncio
async def test_admin_can_configure_base_and_extra_price() -> None:
    plan = make_plan()

    updated = await UpdatePlanPrice(PricingService()).system(
        UpdatePlanPriceDto(plan, 30, Currency.RUB, "700 + 125")
    )

    duration = updated.get_duration(30)
    assert duration is not None
    assert duration.get_price(Currency.RUB) == Decimal("700")
    assert duration.get_extra_device_price(Currency.RUB) == Decimal("125")


@pytest.mark.asyncio
async def test_invalid_device_range_is_rejected() -> None:
    plan = make_plan()

    with pytest.raises(ValueError):
        await UpdatePlanDevice().system(UpdatePlanDeviceDto(plan, "10-4"))


def test_selected_device_snapshot_still_matches_configurable_plan() -> None:
    plan = make_plan()
    snapshot = PlanSnapshotDto.from_plan(plan, duration=30, device_limit=8)

    assert MatchPlan()._is_plan_equal(snapshot, plan)
