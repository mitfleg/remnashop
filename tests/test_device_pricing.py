from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from adaptix import Retort

from src.application.dto import (
    PlanDto,
    PlanDurationDto,
    PlanPriceDto,
    PlanSnapshotDto,
    PriceDetailsDto,
    SubscriptionDto,
    TransactionDto,
    UserDto,
)
from src.application.services import (
    PricingService,
    parse_device_limit_input,
    resolve_initial_device_limit,
)
from src.application.use_cases.gateways.commands.payment import CreatePayment, CreatePaymentDto
from src.application.use_cases.plan.commands.edit import (
    UpdatePlanDevice,
    UpdatePlanDeviceDto,
    UpdatePlanPrice,
    UpdatePlanPriceDto,
)
from src.application.use_cases.plan.queries.match import MatchPlan
from src.application.use_cases.subscription.commands.purchase import (
    PurchaseSubscription,
    PurchaseSubscriptionDto,
)
from src.core.enums import (
    Currency,
    PaymentGatewayType,
    PlanType,
    PurchaseType,
    SubscriptionStatus,
    TransactionStatus,
)
from src.core.utils.converters import decimal_to_plain
from src.core.utils.time import datetime_now


def make_plan() -> PlanDto:
    return PlanDto(
        id=2,
        name="Premium",
        type=PlanType.DEVICES,
        traffic_limit=0,
        device_limit=4,
        max_device_limit=20,
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
        PlanSnapshotDto.from_plan(plan, duration=30, device_limit=21)


@pytest.mark.parametrize("value", ["4", " 8 ", "20"])
def test_device_limit_input_accepts_total_within_plan_range(value: str) -> None:
    plan = make_plan()

    assert parse_device_limit_input(value, plan) == int(value)


@pytest.mark.parametrize("value", [None, "", "abc", "3", "21", "4.5"])
def test_device_limit_input_rejects_invalid_or_out_of_range_value(value: str | None) -> None:
    with pytest.raises(ValueError):
        parse_device_limit_input(value, make_plan())


def test_continue_defaults_to_base_but_renewal_keeps_paid_limit() -> None:
    plan = make_plan()

    assert resolve_initial_device_limit(plan) == 4
    assert resolve_initial_device_limit(plan, preferred=8) == 8
    assert resolve_initial_device_limit(plan, preferred=21) == 4


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


def test_extra_device_price_never_uses_scientific_notation() -> None:
    parsed = PricingService().parse_unit_price("100")

    assert str(parsed) == "100.0000"
    assert decimal_to_plain(parsed) == "100"
    assert decimal_to_plain(Decimal("0.3250")) == "0.325"


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


def test_transaction_history_round_trip_keeps_paid_device_limit() -> None:
    snapshot = PlanSnapshotDto.from_plan(make_plan(), duration=30, device_limit=8)
    transaction = TransactionDto(
        payment_id=uuid4(),
        user_id=1,
        status=TransactionStatus.COMPLETED,
        purchase_type=PurchaseType.RENEW,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto(
            original_amount=Decimal("1100"),
            discount_percent=0,
            final_amount=Decimal("1100"),
        ),
        currency=Currency.RUB,
        plan_snapshot=snapshot,
    )
    retort = Retort(strict_coercion=False)

    stored = retort.dump(transaction)
    restored = retort.load(stored, TransactionDto)

    assert stored["plan_snapshot"]["device_limit"] == 8
    assert restored.plan_snapshot.device_limit == 8
    assert restored.pricing.final_amount == Decimal("1100")


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commit = AsyncMock()

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def make_transaction(plan: PlanSnapshotDto, purchase_type: PurchaseType) -> TransactionDto:
    return TransactionDto(
        payment_id=uuid4(),
        user_id=1,
        status=TransactionStatus.COMPLETED,
        purchase_type=purchase_type,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto(
            original_amount=Decimal("1100"),
            discount_percent=0,
            final_amount=Decimal("1100"),
        ),
        currency=Currency.RUB,
        plan_snapshot=plan,
    )


@pytest.mark.asyncio
async def test_new_purchase_applies_paid_limit_to_subscription_and_remnawave() -> None:
    plan = PlanSnapshotDto.from_plan(make_plan(), duration=30, device_limit=8)
    user = UserDto(id=1, telegram_id=100, name="Test")
    transaction = make_transaction(plan, PurchaseType.NEW)
    remna_user = SimpleNamespace(
        uuid=uuid4(),
        status=SubscriptionStatus.ACTIVE,
        expire_at=datetime_now() + timedelta(days=30),
        subscription_url="https://example.test/sub",
    )
    remnawave = SimpleNamespace(
        create_user=AsyncMock(return_value=remna_user),
        update_user=AsyncMock(),
    )
    subscription_dao = SimpleNamespace(create=AsyncMock(), update=AsyncMock())
    purchase = PurchaseSubscription(
        uow=FakeUnitOfWork(),
        user_dao=SimpleNamespace(
            set_trial_available=AsyncMock(),
            update=AsyncMock(),
        ),
        subscription_dao=subscription_dao,
        remnawave=remnawave,
    )

    await purchase.system(PurchaseSubscriptionDto(user, transaction, None))

    remnawave.create_user.assert_awaited_once_with(user, plan=plan)
    created_subscription = subscription_dao.create.await_args.kwargs["subscription"]
    assert created_subscription.device_limit == 8
    assert created_subscription.plan_snapshot.device_limit == 8


@pytest.mark.asyncio
async def test_renewal_keeps_paid_limit_and_updates_subscription_snapshot() -> None:
    current_plan = PlanSnapshotDto.from_plan(make_plan(), duration=30, device_limit=8)
    renewed_plan = PlanSnapshotDto.from_plan(make_plan(), duration=30, device_limit=8)
    user = UserDto(id=1, telegram_id=100, name="Test")
    old_expire = datetime_now() + timedelta(days=10)
    subscription = SubscriptionDto(
        id=1,
        user_id=user.id,
        user_remna_id=uuid4(),
        status=SubscriptionStatus.ACTIVE,
        is_trial=False,
        traffic_limit=current_plan.traffic_limit,
        device_limit=current_plan.device_limit,
        traffic_limit_strategy=current_plan.traffic_limit_strategy,
        expire_at=old_expire,
        url="https://example.test/sub",
        plan_snapshot=current_plan,
    )
    transaction = make_transaction(renewed_plan, PurchaseType.RENEW)
    remnawave = SimpleNamespace(create_user=AsyncMock(), update_user=AsyncMock())
    subscription_dao = SimpleNamespace(create=AsyncMock(), update=AsyncMock())
    purchase = PurchaseSubscription(
        uow=FakeUnitOfWork(),
        user_dao=SimpleNamespace(
            set_trial_available=AsyncMock(),
            update=AsyncMock(),
        ),
        subscription_dao=subscription_dao,
        remnawave=remnawave,
    )

    await purchase.system(PurchaseSubscriptionDto(user, transaction, subscription))

    assert subscription.device_limit == 8
    assert subscription.plan_snapshot.device_limit == 8
    assert subscription.expire_at == old_expire + timedelta(days=30)
    remnawave.update_user.assert_awaited_once()
    assert remnawave.update_user.await_args.kwargs["subscription"].device_limit == 8
    subscription_dao.update.assert_awaited_once_with(subscription)


@pytest.mark.asyncio
async def test_free_pending_payment_reuse_matches_purchase_type_and_device_limit() -> None:
    plan = PlanSnapshotDto.from_plan(make_plan(), duration=30, device_limit=8)
    user = UserDto(id=1, telegram_id=100, name="Test")
    pending = make_transaction(plan, PurchaseType.RENEW)
    transaction_dao = SimpleNamespace(
        get_recent_pending=AsyncMock(return_value=pending),
        create=AsyncMock(),
    )
    gateway = SimpleNamespace(
        data=SimpleNamespace(
            type=PaymentGatewayType.YOOKASSA,
            currency=Currency.RUB,
        ),
        handle_create_payment=AsyncMock(),
    )
    create_payment = CreatePayment(
        uow=FakeUnitOfWork(),
        payment_gateway_dao=SimpleNamespace(),
        transaction_dao=transaction_dao,
        get_payment_gateway_instance=SimpleNamespace(system=AsyncMock(return_value=gateway)),
        translator_hub=SimpleNamespace(
            get_translator_by_locale=lambda locale: SimpleNamespace(get=lambda *args, **kwargs: "")
        ),
    )

    result = await create_payment._execute(
        user,
        CreatePaymentDto(
            plan_snapshot=plan,
            pricing=PriceDetailsDto(
                original_amount=Decimal("1100"),
                discount_percent=100,
                final_amount=Decimal("0"),
            ),
            purchase_type=PurchaseType.RENEW,
            gateway_type=PaymentGatewayType.YOOKASSA,
        ),
    )

    assert result.id == pending.payment_id
    transaction_dao.get_recent_pending.assert_awaited_once_with(
        user_id=user.id,
        plan_id=plan.id,
        duration_days=plan.duration,
        gateway_type=PaymentGatewayType.YOOKASSA,
        purchase_type=PurchaseType.RENEW,
        device_limit=8,
    )
    gateway.handle_create_payment.assert_not_awaited()
