"""Gift dialog navigation and the temporary native Telegram recipient picker."""

import secrets
from contextlib import suppress
from html import escape
from typing import Any
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram_dialog import DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.kbd import Button, Select
from dishka import FromDishka
from dishka.integrations.aiogram import inject
from dishka.integrations.aiogram_dialog import inject as inject_dialog
from redis.asyncio import Redis
from sqlalchemy import select

from src.application.common import TranslatorRunner
from src.application.common.dao import PaymentGatewayDao
from src.application.dto import PlanDto, TelegramUserDto
from src.application.use_cases.gateways.commands.payment import CreatePayment, CreatePaymentDto
from src.core.constants import USER_KEY
from src.core.enums import PaymentGatewayType, PurchaseType
from src.core.exceptions import PriceNotFoundError
from src.infrastructure.database.models import Gift
from src.infrastructure.services.gifts import GiftError, GiftService, gift_period, gift_status
from src.telegram.states import Gifts, MainMenu

router = Router(name=__name__)
CANCEL_GIFT = "❌ Отменить подарок"
GIFT_GATEWAYS = (PaymentGatewayType.TELEGRAM_STARS, PaymentGatewayType.YOOKASSA)


def picker_key(bot_id: int, user_id: int) -> str:
    return f"gift:picker:{bot_id}:{user_id}"


async def start_picker(bot: Bot, user: TelegramUserDto, redis: Redis) -> None:
    request_id = secrets.randbelow(2**31 - 1) + 1
    key = picker_key(bot.id, user.id)
    await redis.set(key, str(request_id), ex=900)
    prompt = await bot.send_message(
        user.telegram_id,
        "Нажмите «Выбрать получателя» на клавиатуре Telegram.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="👤 Выбрать получателя",
                        request_users=KeyboardButtonRequestUsers(
                            request_id=request_id,
                            user_is_bot=False,
                            max_quantity=1,
                            request_name=True,
                            request_username=True,
                        ),
                    ),
                ],
                [KeyboardButton(text=CANCEL_GIFT)],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
            input_field_placeholder="Выберите получателя подарка",
        ),
    )
    # Keep the message reference longer than the request, so expired pickers can be cleaned up.
    await redis.set(f"{key}:message", str(prompt.message_id), ex=172800)


async def close_picker(bot: Bot, user: TelegramUserDto, redis: Redis) -> None:
    key = picker_key(bot.id, user.id)
    message_id = await redis.getdel(f"{key}:message")
    await redis.delete(key)
    if not message_id:
        return
    # Telegram can remove a reply keyboard only through a new message. Both temporary
    # messages are deleted; the dialog keeps and edits its original menu message.
    notice = await bot.send_message(
        user.telegram_id, "Выбор получателя завершён.", reply_markup=ReplyKeyboardRemove()
    )
    for temporary_id in (int(message_id), notice.message_id):
        with suppress(TelegramBadRequest):
            await bot.delete_message(user.telegram_id, temporary_id)


async def open_screen(
    manager: DialogManager, state: State, *, render: bool = False, **data: Any
) -> None:
    if manager.has_context() and manager.current_context().state.group is Gifts:
        manager.dialog_data.update(data)
        await manager.switch_to(state, show_mode=ShowMode.EDIT)
        if render:
            await manager.show()
    else:
        await manager.start(state, data=data, mode=StartMode.RESET_STACK, show_mode=ShowMode.EDIT)


async def on_dialog_start(data: Any, manager: DialogManager) -> None:
    manager.dialog_data.update(data or {})


@inject_dialog
async def on_gift_start(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
    bot: FromDishka[Bot],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    await close_picker(bot, user, redis)
    await open_screen(dialog_manager, Gifts.PICKER, error="", tab="sent")
    await start_picker(bot, user, redis)


@inject_dialog
async def on_gift_history(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
    bot: FromDishka[Bot],
) -> None:
    await close_picker(bot, dialog_manager.middleware_data[USER_KEY], redis)
    tab = dialog_manager.dialog_data.get("tab", "sent")
    await open_screen(dialog_manager, Gifts.HISTORY, tab=tab, page=0, error="")


@inject_dialog
async def on_main_menu(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
    bot: FromDishka[Bot],
) -> None:
    await close_picker(bot, dialog_manager.middleware_data[USER_KEY], redis)
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK, show_mode=ShowMode.EDIT)


@router.message(F.chat.type == "private", F.users_shared)
@inject
async def on_recipient(
    message: Message,
    user: TelegramUserDto,
    gifts: FromDishka[GiftService],
    redis: FromDishka[Redis],
    bot: Bot,
    dialog_manager: DialogManager,
) -> None:
    shared = message.users_shared
    expected = await redis.get(picker_key(bot.id, user.id))
    if not shared or not expected or shared.request_id != int(expected) or len(shared.users) != 1:
        await close_picker(bot, user, redis)
        await open_screen(
            dialog_manager,
            Gifts.PICKER,
            render=True,
            error="Выбор получателя устарел. Нажмите «Выбрать получателя» ещё раз.",
        )
        return
    recipient = shared.users[0]
    name = " ".join(filter(None, [recipient.first_name, recipient.last_name]))
    name = name or (f"@{recipient.username}" if recipient.username else str(recipient.user_id))
    try:
        gift = await gifts.create(user, recipient.user_id, name, recipient.username)
    except GiftError as error:
        await open_screen(dialog_manager, Gifts.PICKER, render=True, error=str(error))
        return
    await close_picker(bot, user, redis)
    with suppress(TelegramBadRequest):
        await message.delete()
    await open_screen(dialog_manager, Gifts.RECIPIENT, render=True, gift_id=gift.id, error="")


@router.message(F.chat.type == "private", F.text.in_({CANCEL_GIFT, "Отменить подарок"}))
@inject
async def on_cancel(
    message: Message,
    user: TelegramUserDto,
    redis: FromDishka[Redis],
    dialog_manager: DialogManager,
    bot: Bot,
) -> None:
    await close_picker(bot, user, redis)
    with suppress(TelegramBadRequest):
        await message.delete()
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK, show_mode=ShowMode.EDIT)


async def access_plan(gifts: GiftService, i18n: TranslatorRunner) -> PlanDto:
    plans = [
        p for p in await gifts.available_plans() if i18n.get(p.name).strip().casefold() == "доступ"
    ]
    if len(plans) != 1 or not plans[0].durations:
        raise GiftError("Тариф «Доступ» сейчас недоступен. Попробуйте позже.")
    return plans[0]


async def draft_plan(manager: DialogManager, gifts: GiftService, i18n: TranslatorRunner) -> PlanDto:
    user = manager.middleware_data[USER_KEY]
    gift_id = manager.dialog_data["gift_id"]
    await gifts.get(user, gift_id, buyer_only=True)
    if await gifts.transaction(gift_id):
        raise GiftError("Для подарка уже создан счёт. Откройте его в «Мои подарки».")
    return await access_plan(gifts, i18n)


@inject_dialog
async def on_choose_days(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    gifts: FromDishka[GiftService],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    try:
        plan = await draft_plan(dialog_manager, gifts, i18n)
        await open_screen(dialog_manager, Gifts.DURATION, plan_id=plan.id)
    except GiftError as error:
        await callback.answer(str(error), show_alert=True)


@inject_dialog
async def on_duration_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    days: int,
    gifts: FromDishka[GiftService],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    try:
        plan = await draft_plan(dialog_manager, gifts, i18n)
        if plan.id != dialog_manager.dialog_data.get("plan_id") or not plan.get_duration(days):
            raise GiftError("Сроки тарифа изменились. Выберите количество дней заново.")
        await open_screen(dialog_manager, Gifts.PAYMENT_METHOD, days=days)
    except GiftError as error:
        await callback.answer(str(error), show_alert=True)


@inject_dialog
async def on_payment_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    gateway_type: PaymentGatewayType,
    gifts: FromDishka[GiftService],
    gateways: FromDishka[PaymentGatewayDao],
    create_payment: FromDishka[CreatePayment],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    gift_id = dialog_manager.dialog_data["gift_id"]
    try:
        await gifts.get(user, gift_id, buyer_only=True)
        if await gifts.transaction(gift_id):
            await open_screen(dialog_manager, Gifts.CARD)
            return
        plan = await access_plan(gifts, i18n)
        if plan.id != dialog_manager.dialog_data.get("plan_id"):
            raise GiftError("Тариф изменился. Выберите количество дней заново.")
        gateway = next(
            (
                g
                for g in await gateways.get_active()
                if g.type == gateway_type and g.type in GIFT_GATEWAYS
            ),
            None,
        )
        if not gateway:
            raise GiftError("Способ оплаты больше недоступен.")
        snapshot, price = await gifts.quote(
            user, gift_id, plan.id, dialog_manager.dialog_data["days"], gateway.currency
        )
        await create_payment(
            user,
            CreatePaymentDto(
                plan_snapshot=snapshot,
                pricing=price,
                purchase_type=PurchaseType.NEW,
                gateway_type=gateway_type,
                gift_id=gift_id,
            ),
        )
        await open_screen(dialog_manager, Gifts.CARD)
    except (GiftError, PriceNotFoundError) as error:
        await gifts.session.rollback()
        text = (
            str(error) if isinstance(error, GiftError) else "Цена изменилась. Выберите срок заново."
        )
        await callback.answer(text, show_alert=True)


async def on_history_tab(callback: CallbackQuery, widget: Button, manager: DialogManager) -> None:
    await open_screen(manager, Gifts.HISTORY, tab=widget.widget_id, page=0, error="")


async def on_history_page(callback: CallbackQuery, widget: Button, manager: DialogManager) -> None:
    page = manager.dialog_data.get("page", 0) + (1 if widget.widget_id == "next" else -1)
    await open_screen(manager, Gifts.HISTORY, page=max(0, page))


async def on_open_card(
    callback: CallbackQuery, widget: Select, manager: DialogManager, gift_id: int
) -> None:
    await open_screen(manager, Gifts.CARD, gift_id=gift_id)


async def on_refresh(callback: CallbackQuery, widget: Button, manager: DialogManager) -> None:
    manager.show_mode = ShowMode.EDIT


async def history_data(manager: DialogManager, user: TelegramUserDto, gifts: GiftService) -> dict:
    received = manager.dialog_data.get("tab") == "received"
    page = manager.dialog_data.get("page", 0)
    items = await gifts.list_for(user, received=received, offset=page * 10)
    title = "📥 Полученные подарки" if received else "📤 Отправленные подарки"
    return {
        "text": f"<b>{title}</b>\n\n"
        + (
            "Выберите подарок, чтобы открыть подписку или счёт."
            if items
            else "Здесь пока нет подарков."
        ),
        "items": [
            {
                "id": g.id,
                "label": f"🎁 {g.recipient_name if not received else 'Подарок'}"
                f" · {gift_status(g, t)}"[:100],
            }
            for g, t in items
        ],
        "previous_page": page > 0,
        "next_page": len(items) == 10,
    }


async def checkout_data(
    manager: DialogManager,
    user: TelegramUserDto,
    gifts: GiftService,
    gateways: PaymentGatewayDao,
    i18n: TranslatorRunner,
) -> dict:
    gift_id = manager.dialog_data["gift_id"]
    gift = await gifts.get(user, gift_id, buyer_only=True)
    plan = await access_plan(gifts, i18n)
    header = (
        f"🎁 <b>Подарок для {escape(gift.recipient_name)}</b>\n"
        f"Telegram ID: <code>{gift.recipient_telegram_id}</code>\n"
        f"Тариф: {escape(i18n.get(plan.name))}\n"
        f"Устройства: {plan.device_limit or 'Без лимита'}\n\n"
    )
    if await gifts.transaction(gift_id):
        raise GiftError("Для подарка уже создан счёт. Откройте его в «Мои подарки».")
    state = manager.current_context().state
    if state == Gifts.RECIPIENT:
        return {"text": header + "Получатель выбран. Теперь выберите количество дней."}
    if state == Gifts.DURATION:
        currency = (await gifts.settings.get()).default_currency
        durations = []
        for duration in plan.durations:
            try:
                _, price = await gifts.quote(user, gift_id, plan.id, duration.days, currency)
            except PriceNotFoundError:
                continue
            durations.append(
                {
                    "days": duration.days,
                    "period": gift_period(duration.days),
                    "final_amount": price.final_amount,
                    "currency": currency.symbol,
                }
            )
        return {
            "text": header
            + (
                "Выберите срок подписки."
                if durations
                else "Сейчас нет доступных сроков для подарка."
            ),
            "durations": durations,
        }
    days = manager.dialog_data["days"]
    if plan.id != manager.dialog_data.get("plan_id") or not plan.get_duration(days):
        raise GiftError("Сроки тарифа изменились. Выберите количество дней заново.")
    methods = []
    for gateway in await gateways.get_active():
        if gateway.type not in GIFT_GATEWAYS:
            continue
        try:
            _, price = await gifts.quote(user, gift_id, plan.id, days, gateway.currency)
        except PriceNotFoundError:
            continue
        name = gateway.settings.display_name if gateway.settings else None
        name = name or i18n.get("gateway-type", gateway_type=gateway.type)
        icon = "⭐" if gateway.type == PaymentGatewayType.TELEGRAM_STARS else "💳"
        methods.append(
            {
                "gateway_type": gateway.type,
                "gateway_title": f"{icon} {name}",
                "final_amount": price.final_amount,
                "original_amount": price.original_amount,
                "discount_percent": price.discount_percent,
                "currency": gateway.currency.symbol,
            }
        )
    return {
        "text": header + f"Срок: <b>{gift_period(days)}</b>\n\n"
        "Если у получателя есть подписка, добавим к ней выбранное количество дней. "
        "Её параметры и ссылка сохранятся. Для новой подписки срок начнётся после оплаты. "
        "Ссылка подключения будет доступна вам и получателю.\n\n"
        + ("Выберите способ оплаты." if methods else "Сейчас нет доступных способов оплаты."),
        "payment_methods": methods,
    }


@inject_dialog
async def gift_getter(
    dialog_manager: DialogManager,
    gifts: FromDishka[GiftService],
    gateways: FromDishka[PaymentGatewayDao],
    i18n: FromDishka[TranslatorRunner],
    **kwargs: Any,
) -> dict[str, Any]:
    user = dialog_manager.middleware_data[USER_KEY]
    state = dialog_manager.current_context().state
    data: dict[str, Any] = {
        "valid": True,
        "draft": False,
        "items": [],
        "durations": [],
        "payment_methods": [],
        "links": [],
        "previous_page": False,
        "next_page": False,
    }
    try:
        if state == Gifts.PICKER:
            data["text"] = (
                "🎁 <b>Подарить VPN</b>\n\nВыберите человека в Telegram, затем количество дней "
                "и способ оплаты. Получателю не обязательно уже быть в боте.\n\n"
                "После оплаты вы оба сможете открыть ссылку подключения в «Мои подарки». "
                "Действующая подписка продлится, её параметры и ссылка сохранятся."
            )
            if error := dialog_manager.dialog_data.get("error"):
                data["text"] += f"\n\n{escape(error)}"
        elif state == Gifts.HISTORY:
            data.update(await history_data(dialog_manager, user, gifts))
        elif state == Gifts.CARD:
            gift_id = dialog_manager.dialog_data["gift_id"]
            text, markup = await gifts.card(user, gift_id)
            gift = await gifts.get(user, gift_id)
            dialog_manager.dialog_data["tab"] = "sent" if gift.buyer_id == user.id else "received"
            data.update(text=text, draft=not await gifts.transaction(gift_id))
            data["links"] = [
                {"id": index, "text": b.text, "url": b.url}
                for index, b in enumerate(b for row in markup.inline_keyboard for b in row if b.url)
            ]
        else:
            data.update(await checkout_data(dialog_manager, user, gifts, gateways, i18n))
    except (GiftError, PriceNotFoundError) as error:
        data.update(
            valid=False,
            text=escape(str(error))
            if isinstance(error, GiftError)
            else "Цена изменилась. Выберите количество дней заново.",
        )
    return data


@router.message(Command("gifts"), F.chat.type == "private")
@inject
async def on_gifts_command(
    message: Message,
    user: TelegramUserDto,
    redis: FromDishka[Redis],
    bot: Bot,
    dialog_manager: DialogManager,
) -> None:
    await close_picker(bot, user, redis)
    await open_screen(dialog_manager, Gifts.HISTORY, render=True, tab="sent", page=0)


@router.message(
    CommandStart(deep_link=True),
    F.chat.type == "private",
    F.text.regexp(r"^/start(?:@\w+)? gift_"),
)
@inject
async def on_gift_link(
    message: Message,
    command: CommandObject,
    user: TelegramUserDto,
    gifts: FromDishka[GiftService],
    dialog_manager: DialogManager,
) -> None:
    try:
        token = UUID((command.args or "").removeprefix("gift_"))
        gift_id = await gifts.session.scalar(select(Gift.id).where(Gift.token == token))
        if gift_id is None:
            raise GiftError("Подарок не найден.")
        await gifts.get(user, gift_id)
        await open_screen(dialog_manager, Gifts.CARD, render=True, gift_id=gift_id)
    except (ValueError, GiftError):
        await open_screen(dialog_manager, Gifts.HISTORY, render=True, tab="received", page=0)


@router.callback_query(F.data.startswith("gift:"), F.message.chat.type == "private")
@inject
async def on_gift_callback(
    callback: CallbackQuery,
    user: TelegramUserDto,
    gifts: FromDishka[GiftService],
    redis: FromDishka[Redis],
    bot: Bot,
    dialog_manager: DialogManager,
) -> None:
    """Notifications and buttons sent before the dialog release open the current dialog."""
    parts = (callback.data or "").split(":")
    try:
        await close_picker(bot, user, redis)
        action = parts[1]
        if action == "new":
            await open_screen(dialog_manager, Gifts.PICKER, render=True, error="")
            await start_picker(bot, user, redis)
        elif action == "menu":
            await dialog_manager.start(
                MainMenu.MAIN, mode=StartMode.RESET_STACK, show_mode=ShowMode.EDIT
            )
        elif action == "list":
            if parts[2] not in ("sent", "received") or not 0 <= int(parts[3]) <= 10000:
                raise GiftError("Откройте «Мои подарки» заново.")
            await open_screen(
                dialog_manager, Gifts.HISTORY, render=True, tab=parts[2], page=int(parts[3])
            )
        elif action in ("open", "plans", "plan", "duration", "pay"):
            gift_id = int(parts[2])
            await gifts.get(user, gift_id, buyer_only=action != "open")
            # Old checkout callbacks never create an invoice using stale prices or another plan.
            state = (
                Gifts.CARD
                if action == "open" or await gifts.transaction(gift_id)
                else Gifts.RECIPIENT
            )
            await open_screen(dialog_manager, state, render=True, gift_id=gift_id)
        else:
            raise GiftError("Откройте «Мои подарки» заново.")
        await callback.answer()
    except (GiftError, ValueError, IndexError) as error:
        text = str(error) if isinstance(error, GiftError) else "Откройте «Мои подарки» заново."
        await callback.answer(text, show_alert=True)
