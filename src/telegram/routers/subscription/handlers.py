from typing import Any, Optional, TypedDict, cast

from adaptix import Retort
from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button, Select
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier
from src.application.common.dao import PaymentGatewayDao, PlanDao, SettingsDao, SubscriptionDao
from src.application.dto import PlanDto, PlanSnapshotDto, SubscriptionDto, TelegramUserDto
from src.application.services import PricingService
from src.application.use_cases.gateways.commands.payment import (
    CreatePayment,
    CreatePaymentDto,
    ProcessPayment,
    ProcessPaymentDto,
)
from src.application.use_cases.plan.queries.match import MatchPlan, MatchPlanDto
from src.application.use_cases.user.queries.plans import GetAvailablePlans
from src.core.constants import PAYMENT_PREFIX, USER_KEY
from src.core.enums import PaymentGatewayType, PurchaseType, TransactionStatus
from src.telegram.states import Subscription


async def on_subscription_start(start_data: Any, manager: DialogManager) -> None:
    if not start_data or "trial_plan" not in start_data:
        return

    manager.dialog_data[PlanDto.__name__] = start_data["trial_plan"]
    manager.dialog_data["purchase_type"] = PurchaseType.NEW
    manager.dialog_data["selected_duration"] = start_data["trial_duration"]
    manager.dialog_data["only_single_plan"] = True
    manager.dialog_data["only_single_duration"] = True
    manager.dialog_data["is_free"] = False

    await manager.switch_to(Subscription.PAYMENT_METHOD)


PAYMENT_CACHE_KEY = "payment_cache"
CURRENT_DURATION_KEY = "selected_duration"
CURRENT_METHOD_KEY = "selected_payment_method"
CURRENT_DEVICE_LIMIT_KEY = "selected_device_limit"


class CachedPaymentData(TypedDict):
    payment_id: str
    payment_url: Optional[str]
    final_pricing: str


def _get_cache_key(
    duration: int,
    gateway_type: PaymentGatewayType,
    purchase_type: PurchaseType,
    device_limit: int,
) -> str:
    return f"{purchase_type}:{duration}:{gateway_type.value}:{device_limit}"


def _select_default_device_limit(
    dialog_manager: DialogManager,
    plan: PlanDto,
    preferred: Optional[int] = None,
) -> None:
    selected = preferred
    if selected is None or selected < plan.device_limit or selected > plan.max_device_limit:
        selected = plan.device_limit
    dialog_manager.dialog_data[CURRENT_DEVICE_LIMIT_KEY] = plan.resolve_device_limit(selected)


def _load_payment_data(dialog_manager: DialogManager) -> dict[str, CachedPaymentData]:
    if PAYMENT_CACHE_KEY not in dialog_manager.dialog_data:
        dialog_manager.dialog_data[PAYMENT_CACHE_KEY] = {}
    return cast(dict[str, CachedPaymentData], dialog_manager.dialog_data[PAYMENT_CACHE_KEY])


def _save_payment_data(dialog_manager: DialogManager, payment_data: CachedPaymentData) -> None:
    dialog_manager.dialog_data["payment_id"] = payment_data["payment_id"]
    dialog_manager.dialog_data["payment_url"] = payment_data["payment_url"]
    dialog_manager.dialog_data["final_pricing"] = payment_data["final_pricing"]


async def _create_payment_and_get_data(
    dialog_manager: DialogManager,
    plan: PlanDto,
    duration_days: int,
    gateway_type: PaymentGatewayType,
    retort: Retort,
    payment_gateway_dao: PaymentGatewayDao,
    notifier: Notifier,
    pricing_service: PricingService,
    create_payment: CreatePayment,
) -> Optional[CachedPaymentData]:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    duration = plan.get_duration(duration_days)
    payment_gateway = await payment_gateway_dao.get_by_type(gateway_type)
    purchase_type: PurchaseType = dialog_manager.dialog_data["purchase_type"]
    selected_device_limit = plan.resolve_device_limit(
        dialog_manager.dialog_data.get(CURRENT_DEVICE_LIMIT_KEY)
    )

    if not duration or not payment_gateway:
        logger.error(f"{user.log} Failed to find duration or gateway for payment creation")
        return None

    transaction_plan = PlanSnapshotDto.from_plan(
        plan,
        duration.days,
        device_limit=selected_device_limit,
    )
    pricing = pricing_service.calculate_for_duration(
        user,
        duration,
        payment_gateway.currency,
        apply_discount=not plan.is_trial,
        base_device_limit=plan.device_limit,
        selected_device_limit=selected_device_limit,
    )

    try:
        result = await create_payment(
            user,
            CreatePaymentDto(
                plan_snapshot=transaction_plan,
                pricing=pricing,
                purchase_type=purchase_type,
                gateway_type=gateway_type,
            ),
        )

        return CachedPaymentData(
            payment_id=str(result.id),
            payment_url=result.url,
            final_pricing=retort.dump(pricing),
        )

    except Exception:
        logger.error(f"{user.log} Failed to create payment")
        await notifier.notify_user(user, i18n_key="ntf-subscription.payment-creation-failed")
        raise


async def _resolve_renew_plan(
    user: TelegramUserDto,
    dialog_manager: DialogManager,
    current_subscription: Optional[SubscriptionDto],
    plans: list[PlanDto],
    match_plan: MatchPlan,
    notifier: Notifier,
    retort: Retort,
) -> bool:
    if not current_subscription:
        return False

    matched_plan = await match_plan.system(
        MatchPlanDto(plan_snapshot=current_subscription.plan_snapshot, plans=plans)
    )
    if matched_plan:
        dialog_manager.dialog_data[PlanDto.__name__] = retort.dump(matched_plan)
        dialog_manager.dialog_data["only_single_plan"] = True
        dialog_manager.dialog_data["plan_is_modified"] = False
        if matched_plan.has_device_selection:
            _select_default_device_limit(
                dialog_manager,
                matched_plan,
                current_subscription.device_limit,
            )
            await dialog_manager.switch_to(state=Subscription.DEVICES)
        else:
            _select_default_device_limit(dialog_manager, matched_plan)
            await dialog_manager.switch_to(state=Subscription.DURATION)
        return True

    snapshot_id = current_subscription.plan_snapshot.id
    modified_plan = next((p for p in plans if p.id == snapshot_id), None)
    if modified_plan:
        logger.info(
            f"{user.log} Plan '{snapshot_id}' was modified, allowing renewal with updated data"
        )
        dialog_manager.dialog_data[PlanDto.__name__] = retort.dump(modified_plan)
        dialog_manager.dialog_data["only_single_plan"] = True
        dialog_manager.dialog_data["plan_is_modified"] = True
        if modified_plan.has_device_selection:
            _select_default_device_limit(
                dialog_manager,
                modified_plan,
                current_subscription.device_limit,
            )
            await dialog_manager.switch_to(state=Subscription.DEVICES)
        else:
            _select_default_device_limit(dialog_manager, modified_plan)
            await dialog_manager.switch_to(state=Subscription.DURATION)
        return True

    logger.warning(f"{user.log} Tried to renew, but no matching plan found")
    await notifier.notify_user(user, i18n_key="ntf-subscription.renew-plan-unavailable")
    return True


@inject
async def on_purchase_type_select(
    purchase_type: PurchaseType,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    subscription_dao: FromDishka[SubscriptionDao],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    notifier: FromDishka[Notifier],
    match_plan: FromDishka[MatchPlan],
    get_available_plans: FromDishka[GetAvailablePlans],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    plans: list[PlanDto] = await get_available_plans.system(user)
    gateways = await payment_gateway_dao.get_active()
    dialog_manager.dialog_data["purchase_type"] = purchase_type
    dialog_manager.dialog_data.pop(CURRENT_DURATION_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_DEVICE_LIMIT_KEY, None)
    dialog_manager.dialog_data.pop(PAYMENT_CACHE_KEY, None)

    if not plans:
        logger.warning(f"{user.log} No available subscription plans")
        await notifier.notify_user(user, i18n_key="ntf-subscription.plans-unavailable")
        return

    if not gateways:
        logger.warning(f"{user.log} No active payment gateways")
        await notifier.notify_user(user, i18n_key="ntf-subscription.gateways-unavailable")
        return

    current_subscription = await subscription_dao.get_current(user.id)

    if purchase_type == PurchaseType.RENEW:
        if await _resolve_renew_plan(
            user, dialog_manager, current_subscription, plans, match_plan, notifier, retort
        ):
            return

    if len(plans) == 1:
        logger.info(f"{user.log} Auto-selected single plan '{plans[0].id}'")
        dialog_manager.dialog_data[PlanDto.__name__] = retort.dump(plans[0])
        dialog_manager.dialog_data["only_single_plan"] = True
        _select_default_device_limit(dialog_manager, plans[0])
        await dialog_manager.switch_to(
            state=(Subscription.DEVICES if plans[0].has_device_selection else Subscription.DURATION)
        )
        return

    dialog_manager.dialog_data["only_single_plan"] = False
    await dialog_manager.switch_to(state=Subscription.PLANS)


@inject
async def on_subscription_plans(  # noqa: C901
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    subscription_dao: FromDishka[SubscriptionDao],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    pricing_service: FromDishka[PricingService],
    notifier: FromDishka[Notifier],
    match_plan: FromDishka[MatchPlan],
    get_available_plans: FromDishka[GetAvailablePlans],
    create_payment: FromDishka[CreatePayment],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    logger.info(f"{user.log} Opened subscription plans menu")

    plans: list[PlanDto] = await get_available_plans.system(user)
    gateways = await payment_gateway_dao.get_active()

    if not callback.data:
        raise ValueError("Callback data is empty")

    purchase_type = PurchaseType(callback.data.removeprefix(PAYMENT_PREFIX))
    dialog_manager.dialog_data["purchase_type"] = purchase_type

    dialog_manager.dialog_data.pop(CURRENT_DURATION_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_DEVICE_LIMIT_KEY, None)
    dialog_manager.dialog_data.pop(PAYMENT_CACHE_KEY, None)

    if not plans:
        logger.warning(f"{user.log} No available subscription plans")
        await notifier.notify_user(user, i18n_key="ntf-subscription.plans-unavailable")
        return

    if not gateways:
        logger.warning(f"{user.log} No active payment gateways")
        await notifier.notify_user(user, i18n_key="ntf-subscription.gateways-unavailable")
        return

    current_subscription = await subscription_dao.get_current(user.id)

    if purchase_type == PurchaseType.RENEW:
        if await _resolve_renew_plan(
            user, dialog_manager, current_subscription, plans, match_plan, notifier, retort
        ):
            return

    if len(plans) == 1:
        logger.info(f"{user.log} Auto-selected single plan '{plans[0].id}'")
        dialog_manager.dialog_data[PlanDto.__name__] = retort.dump(plans[0])
        dialog_manager.dialog_data["only_single_plan"] = True

        _select_default_device_limit(dialog_manager, plans[0])
        if plans[0].has_device_selection:
            await dialog_manager.switch_to(state=Subscription.DEVICES)
            return

        if len(plans[0].durations) == 1:
            logger.info(f"{user.log} Auto-selected duration '{plans[0].durations[0].days}'")
            dialog_manager.dialog_data["selected_duration"] = plans[0].durations[0].days
            dialog_manager.dialog_data["only_single_duration"] = True

            if len(gateways) == 1:
                logger.info(f"{user.log} Auto-selected payment method '{gateways[0].type}'")
                dialog_manager.dialog_data["selected_payment_method"] = gateways[0].type
                dialog_manager.dialog_data["only_single_payment_method"] = True

                payment_data = await _create_payment_and_get_data(
                    dialog_manager=dialog_manager,
                    plan=plans[0],
                    duration_days=plans[0].durations[0].days,
                    gateway_type=gateways[0].type,
                    retort=retort,
                    payment_gateway_dao=payment_gateway_dao,
                    notifier=notifier,
                    pricing_service=pricing_service,
                    create_payment=create_payment,
                )

                if payment_data:
                    _save_payment_data(dialog_manager, payment_data)

                await dialog_manager.switch_to(state=Subscription.CONFIRM)
                return

            await dialog_manager.switch_to(state=Subscription.PAYMENT_METHOD)
            return

        await dialog_manager.switch_to(state=Subscription.DURATION)
        return

    dialog_manager.dialog_data["only_single_plan"] = False
    dialog_manager.dialog_data["only_single_duration"] = False
    await dialog_manager.switch_to(state=Subscription.PLANS)


@inject
async def on_plan_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_plan: int,
    retort: FromDishka[Retort],
    plan_dao: FromDishka[PlanDao],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    plan = await plan_dao.get_by_id(plan_id=selected_plan)

    if not plan:
        logger.error(f"{user.log} Selected plan with id '{selected_plan}', but it was not found")
        await dialog_manager.start(state=Subscription.MAIN)
        return

    logger.info(f"{user.log} Selected plan '{plan.id}'")

    dialog_manager.dialog_data[PlanDto.__name__] = retort.dump(plan)
    dialog_manager.dialog_data.pop(PAYMENT_CACHE_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_DURATION_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_METHOD_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_DEVICE_LIMIT_KEY, None)

    _select_default_device_limit(dialog_manager, plan)
    if plan.has_device_selection:
        await dialog_manager.switch_to(state=Subscription.DEVICES)
        return

    if len(plan.durations) == 1:
        logger.info(f"{user.log} Auto-selected single duration '{plan.durations[0].days}'")
        dialog_manager.dialog_data["only_single_duration"] = True
        await on_duration_select(callback, widget, dialog_manager, plan.durations[0].days)  # type:ignore[no-untyped-call]
        return

    dialog_manager.dialog_data["only_single_duration"] = False
    await dialog_manager.switch_to(state=Subscription.DURATION)


@inject
async def on_device_limit_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_device_limit: int,
    retort: FromDishka[Retort],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    raw_plan = dialog_manager.dialog_data.get(PlanDto.__name__)
    if not raw_plan:
        logger.error("PlanDto not found in dialog data")
        await dialog_manager.start(state=Subscription.MAIN)
        return

    plan = retort.load(raw_plan, PlanDto)
    selected = plan.resolve_device_limit(selected_device_limit)
    dialog_manager.dialog_data[CURRENT_DEVICE_LIMIT_KEY] = selected
    dialog_manager.dialog_data.pop(PAYMENT_CACHE_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_DURATION_KEY, None)
    dialog_manager.dialog_data.pop(CURRENT_METHOD_KEY, None)
    logger.info(f"{user.log} Selected subscription device limit '{selected}'")

    if len(plan.durations) == 1:
        dialog_manager.dialog_data["only_single_duration"] = True
        await on_duration_select(  # type: ignore[no-untyped-call]
            callback,
            widget,
            dialog_manager,
            plan.durations[0].days,
        )
        return

    dialog_manager.dialog_data["only_single_duration"] = False
    await dialog_manager.switch_to(state=Subscription.DURATION)


@inject
async def on_duration_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_duration: int,
    retort: FromDishka[Retort],
    settings_dao: FromDishka[SettingsDao],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    notifier: FromDishka[Notifier],
    pricing_service: FromDishka[PricingService],
    create_payment: FromDishka[CreatePayment],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    logger.info(f"{user.log} Selected subscription duration '{selected_duration}' days")
    dialog_manager.dialog_data[CURRENT_DURATION_KEY] = selected_duration

    raw_plan = dialog_manager.dialog_data.get(PlanDto.__name__)

    if not raw_plan:
        logger.error("PlanDto not found in dialog data")
        await dialog_manager.start(state=Subscription.MAIN)
        return

    plan = retort.load(raw_plan, PlanDto)
    duration = plan.get_duration(selected_duration)
    if duration is None:
        logger.warning(f"{user.log} duration '{selected_duration}' missing (stale dialog data)")
        await dialog_manager.start(state=Subscription.MAIN)
        return
    settings = await settings_dao.get()
    gateways = await payment_gateway_dao.get_active()
    currency = settings.default_currency
    selected_device_limit = plan.resolve_device_limit(
        dialog_manager.dialog_data.get(CURRENT_DEVICE_LIMIT_KEY)
    )
    price = pricing_service.calculate_for_duration(
        user,
        duration,
        currency,
        apply_discount=not plan.is_trial,
        base_device_limit=plan.device_limit,
        selected_device_limit=selected_device_limit,
    )
    dialog_manager.dialog_data["is_free"] = price.is_free

    if len(gateways) == 1 or price.is_free:
        selected_payment_method = gateways[0].type
        dialog_manager.dialog_data[CURRENT_METHOD_KEY] = selected_payment_method

        purchase_type: PurchaseType = dialog_manager.dialog_data["purchase_type"]
        cache = _load_payment_data(dialog_manager)
        cache_key = _get_cache_key(
            selected_duration,
            selected_payment_method,
            purchase_type,
            selected_device_limit,
        )

        if cache_key in cache:
            logger.info(f"{user.log} Re-selected same duration and single gateway")
            _save_payment_data(dialog_manager, cache[cache_key])
            await dialog_manager.switch_to(state=Subscription.CONFIRM)
            return

        logger.info(f"{user.log} Auto-selected single gateway '{selected_payment_method}'")

        payment_data = await _create_payment_and_get_data(
            dialog_manager=dialog_manager,
            plan=plan,
            duration_days=selected_duration,
            gateway_type=selected_payment_method,
            retort=retort,
            payment_gateway_dao=payment_gateway_dao,
            notifier=notifier,
            pricing_service=pricing_service,
            create_payment=create_payment,
        )

        if payment_data:
            cache[cache_key] = payment_data
            _save_payment_data(dialog_manager, payment_data)
            await dialog_manager.switch_to(state=Subscription.CONFIRM)
            return

    dialog_manager.dialog_data.pop(CURRENT_METHOD_KEY, None)
    await dialog_manager.switch_to(state=Subscription.PAYMENT_METHOD)


@inject
async def on_payment_method_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_payment_method: PaymentGatewayType,
    retort: FromDishka[Retort],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    notifier: FromDishka[Notifier],
    pricing_service: FromDishka[PricingService],
    create_payment: FromDishka[CreatePayment],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    logger.info(f"{user.log} Selected payment method '{selected_payment_method}'")

    selected_duration = dialog_manager.dialog_data[CURRENT_DURATION_KEY]
    dialog_manager.dialog_data[CURRENT_METHOD_KEY] = selected_payment_method
    purchase_type: PurchaseType = dialog_manager.dialog_data["purchase_type"]
    raw_plan = dialog_manager.dialog_data.get(PlanDto.__name__)
    if not raw_plan:
        logger.error("PlanDto not found in dialog data")
        await dialog_manager.start(state=Subscription.MAIN)
        return
    plan = retort.load(raw_plan, PlanDto)
    selected_device_limit = plan.resolve_device_limit(
        dialog_manager.dialog_data.get(CURRENT_DEVICE_LIMIT_KEY)
    )
    cache = _load_payment_data(dialog_manager)
    cache_key = _get_cache_key(
        selected_duration,
        selected_payment_method,
        purchase_type,
        selected_device_limit,
    )

    if cache_key in cache:
        logger.info(f"{user.log} Re-selected same method and duration")
        _save_payment_data(dialog_manager, cache[cache_key])
        await dialog_manager.switch_to(state=Subscription.CONFIRM)
        return

    logger.info(f"{user.log} New combination. Creating new payment")

    payment_data = await _create_payment_and_get_data(
        dialog_manager=dialog_manager,
        plan=plan,
        duration_days=selected_duration,
        gateway_type=selected_payment_method,
        retort=retort,
        payment_gateway_dao=payment_gateway_dao,
        notifier=notifier,
        pricing_service=pricing_service,
        create_payment=create_payment,
    )

    if payment_data:
        cache[cache_key] = payment_data
        _save_payment_data(dialog_manager, payment_data)

    await dialog_manager.switch_to(state=Subscription.CONFIRM)


@inject
async def on_get_subscription(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    process_payment: FromDishka[ProcessPayment],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    payment_id = dialog_manager.dialog_data["payment_id"]
    gateway_type: PaymentGatewayType = dialog_manager.dialog_data[CURRENT_METHOD_KEY]
    logger.info(f"{user.log} Getted free subscription '{payment_id}'")
    await process_payment.system(
        ProcessPaymentDto(
            payment_id=payment_id,
            new_transaction_status=TransactionStatus.COMPLETED,
            gateway_type=gateway_type,
        ),
    )
