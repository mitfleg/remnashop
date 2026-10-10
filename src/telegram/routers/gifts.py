"""Native Telegram recipient picker and persistent sent/received gift cards."""

import secrets
from html import escape
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram_dialog import DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram import inject
from dishka.integrations.aiogram_dialog import inject as inject_dialog
from redis.asyncio import Redis
from sqlalchemy import select

from src.application.common import TranslatorRunner
from src.application.common.dao import PaymentGatewayDao
from src.application.dto import TelegramUserDto
from src.application.use_cases.gateways.commands.payment import CreatePayment, CreatePaymentDto
from src.core.constants import USER_KEY
from src.core.enums import PaymentGatewayType, PurchaseType
from src.core.exceptions import PriceNotFoundError
from src.infrastructure.database.models import Gift
from src.infrastructure.services.gifts import GiftError, GiftService, gift_period, gift_status
from src.telegram.states import MainMenu

router = Router(name=__name__)
CANCEL_GIFT = "Отменить подарок"


def picker_key(bot_id: int, user_id: int) -> str:
    return f"gift:picker:{bot_id}:{user_id}"


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def start_picker(bot: Bot, user: TelegramUserDto, redis: Redis) -> None:
    request_id = secrets.randbelow(2**31 - 1) + 1
    await redis.set(picker_key(bot.id, user.id), str(request_id), ex=900)
    await bot.send_message(
        user.telegram_id,
        "🎁 <b>Подарить VPN</b>\n\nВыберите человека в Telegram, затем тариф и срок. "
        "Ему не обязательно уже быть в боте.\n\n"
        "После оплаты вы оба сможете открыть ссылку подключения в «Мои подарки». "
        "Срок начнётся после оплаты. Текущая подписка получателя сохранится.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать получателя",
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
            input_field_placeholder="Нажмите «Выбрать получателя»",
        ),
    )


@inject_dialog
async def on_gift_start(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
    bot: FromDishka[Bot],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    await callback.answer()
    await start_picker(bot, user, redis)
    dialog_manager.show_mode = ShowMode.NO_UPDATE


@inject_dialog
async def on_gift_history(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    gifts: FromDishka[GiftService],
    bot: FromDishka[Bot],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    await callback.answer()
    await show_history(bot, user, gifts, "sent", 0)
    dialog_manager.show_mode = ShowMode.NO_UPDATE


async def show_plans(bot: Bot, user: TelegramUserDto, gifts: GiftService, gift_id: int) -> None:
    gift = await gifts.get(user, gift_id, buyer_only=True)
    if await gifts.transaction(gift_id):
        await show_card(bot, user, gifts, gift_id)
        return
    plans = await gifts.available_plans()
    rows = [[button(p.name, f"gift:plan:{gift.id}:{p.id}")] for p in plans]
    rows.append([button("Выбрать другого человека", "gift:new")])
    rows.append([button("Мои подарки", "gift:list:sent:0")])
    text = (
        f"🎁 Подарок для <b>{escape(gift.recipient_name)}</b>\n"
        f"Telegram ID: <code>{gift.recipient_telegram_id}</code>\n\n"
        + ("Выберите тариф." if plans else "Сейчас нет тарифов для подарка. Попробуйте позже.")
    )
    await bot.send_message(user.telegram_id, text, reply_markup=keyboard(rows))


@router.message(F.chat.type == "private", F.users_shared)
@inject
async def on_recipient(
    message: Message,
    user: TelegramUserDto,
    gifts: FromDishka[GiftService],
    redis: FromDishka[Redis],
    bot: Bot,
) -> None:
    shared = message.users_shared
    expected = await redis.get(picker_key(bot.id, user.id))
    if not shared or not expected or shared.request_id != int(expected) or len(shared.users) != 1:
        await message.answer(
            "Выбор получателя устарел. Нажмите «Подарить VPN» ещё раз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    recipient = shared.users[0]
    name = " ".join(filter(None, [recipient.first_name, recipient.last_name]))
    name = name or (f"@{recipient.username}" if recipient.username else str(recipient.user_id))
    try:
        gift = await gifts.create(user, recipient.user_id, name, recipient.username)
    except GiftError as error:
        await message.answer(str(error))
        return
    await redis.delete(picker_key(bot.id, user.id))
    await message.answer(f"Получатель: {escape(name)}", reply_markup=ReplyKeyboardRemove())
    await show_plans(bot, user, gifts, gift.id)


@router.message(F.chat.type == "private", F.text == CANCEL_GIFT)
@inject
async def on_cancel(
    message: Message,
    user: TelegramUserDto,
    redis: FromDishka[Redis],
    dialog_manager: DialogManager,
    bot: Bot,
) -> None:
    await redis.delete(picker_key(bot.id, user.id))
    await message.answer("Выбор получателя отменён.", reply_markup=ReplyKeyboardRemove())
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK)


async def show_card(bot: Bot, user: TelegramUserDto, gifts: GiftService, gift_id: int) -> None:
    text, markup = await gifts.card(user, gift_id)
    await bot.send_message(
        user.telegram_id,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def show_history(
    bot: Bot, user: TelegramUserDto, gifts: GiftService, tab: str, page: int
) -> None:
    received = tab == "received"
    items = await gifts.list_for(user, received=received, offset=page * 10)
    rows = [
        [button("Отправленные", "gift:list:sent:0"), button("Полученные", "gift:list:received:0")]
    ]
    for gift, transaction in items:
        name = gift.recipient_name if not received else "Подарок"
        label = f"{name} · {gift_status(gift, transaction)}"
        rows.append([button(label[:100], f"gift:open:{gift.id}")])
    paging = []
    if page:
        paging.append(button("Назад", f"gift:list:{tab}:{page - 1}"))
    if len(items) == 10:
        paging.append(button("Дальше", f"gift:list:{tab}:{page + 1}"))
    if paging:
        rows.append(paging)
    rows.extend([[button("Подарить VPN", "gift:new")], [button("Главное меню", "gift:menu")]])
    title = "Полученные подарки" if received else "Отправленные подарки"
    text = f"<b>{title}</b>\n\n" + ("Выберите подарок." if items else "Здесь пока нет подарков.")
    await bot.send_message(user.telegram_id, text, reply_markup=keyboard(rows))


@router.message(Command("gifts"), F.chat.type == "private")
@inject
async def on_gifts_command(
    message: Message, user: TelegramUserDto, gifts: FromDishka[GiftService], bot: Bot
) -> None:
    await show_history(bot, user, gifts, "sent", 0)


@router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? gift_"))
@inject
async def on_gift_link(
    message: Message,
    command: CommandObject,
    user: TelegramUserDto,
    gifts: FromDishka[GiftService],
    dialog_manager: DialogManager,
    bot: Bot,
) -> None:
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK)
    try:
        token = UUID((command.args or "").removeprefix("gift_"))
        gift_id = await gifts.session.scalar(select(Gift.id).where(Gift.token == token))
        if gift_id is None:
            raise GiftError("Подарок не найден.")
        await show_card(bot, user, gifts, gift_id)
    except (ValueError, GiftError):
        await message.answer("Подарок не найден или предназначен другому человеку.")


@router.callback_query(F.data.startswith("gift:"))
@inject
async def on_gift_callback(  # noqa: C901
    callback: CallbackQuery,
    user: TelegramUserDto,
    gifts: FromDishka[GiftService],
    gateways: FromDishka[PaymentGatewayDao],
    create_payment: FromDishka[CreatePayment],
    redis: FromDishka[Redis],
    bot: Bot,
    i18n: FromDishka[TranslatorRunner],
    dialog_manager: DialogManager,
) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":")
    action = parts[1]
    try:
        if action == "new":
            await start_picker(bot, user, redis)
        elif action == "menu":
            await redis.delete(picker_key(bot.id, user.id))
            await bot.send_message(
                user.telegram_id, "Главное меню", reply_markup=ReplyKeyboardRemove()
            )
            await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK)
        elif action == "list":
            if parts[2] not in ("sent", "received") or not 0 <= int(parts[3]) <= 10000:
                raise GiftError("Откройте «Мои подарки» заново.")
            await show_history(bot, user, gifts, parts[2], int(parts[3]))
        elif action == "open":
            await show_card(bot, user, gifts, int(parts[2]))
        elif action == "plans":
            await show_plans(bot, user, gifts, int(parts[2]))
        elif action in ("plan", "duration", "pay"):
            gift_id, plan_id = int(parts[2]), int(parts[3])
            gift = await gifts.get(user, gift_id, buyer_only=True)
            if await gifts.transaction(gift_id):
                await show_card(bot, user, gifts, gift_id)
                return
            plan = next((p for p in await gifts.available_plans() if p.id == plan_id), None)
            if not plan:
                raise GiftError("Тариф больше недоступен. Выберите другой.")
            if action == "plan":
                rows = [
                    [button(gift_period(d.days), f"gift:duration:{gift_id}:{plan.id}:{d.days}")]
                    for d in plan.durations
                ]
                rows.append([button("Назад к тарифам", f"gift:plans:{gift_id}")])
                await bot.send_message(
                    user.telegram_id,
                    f"Подарок для <b>{escape(gift.recipient_name)}</b>\n"
                    f"Тариф: {escape(i18n.get(plan.name))}\n"
                    f"Устройства: {plan.device_limit or 'Без лимита'}\n\nВыберите срок.",
                    reply_markup=keyboard(rows),
                )
            elif action == "duration":
                days = int(parts[4])
                rows = []
                for gateway in await gateways.get_active():
                    try:
                        _, price = await gifts.quote(user, gift_id, plan.id, days, gateway.currency)
                    except (PriceNotFoundError, GiftError):
                        continue
                    name = gateway.settings.display_name if gateway.settings else None
                    name = name or i18n.get("gateway-type", gateway_type=gateway.type)
                    rows.append(
                        [
                            button(
                                f"{name} · {price.final_amount} {gateway.currency.symbol}",
                                f"gift:pay:{gift_id}:{plan.id}:{days}:{gateway.type.value}",
                            )
                        ]
                    )
                rows.append([button("Изменить срок", f"gift:plan:{gift_id}:{plan.id}")])
                text = (
                    f"<b>Проверьте подарок</b>\nКому: {escape(gift.recipient_name)}\n"
                    f"Telegram ID: <code>{gift.recipient_telegram_id}</code>\n"
                    f"Тариф: {escape(i18n.get(plan.name))} · {gift_period(days)}\n\n"
                    "Это отдельная подписка. Срок начнётся после оплаты. "
                    "Ссылка подключения будет доступна вам и получателю.\n\n"
                    + (
                        "Выберите способ оплаты."
                        if len(rows) > 1
                        else "Сейчас нет доступных способов оплаты этого тарифа."
                    )
                )
                await bot.send_message(user.telegram_id, text, reply_markup=keyboard(rows))
            else:
                days, gateway_type = int(parts[4]), PaymentGatewayType(parts[5])
                selected_gateway = next(
                    (g for g in await gateways.get_active() if g.type == gateway_type), None
                )
                if not selected_gateway:
                    raise GiftError("Способ оплаты больше недоступен.")
                snapshot, price = await gifts.quote(
                    user, gift_id, plan.id, days, selected_gateway.currency
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
                await show_card(bot, user, gifts, gift_id)
    except (GiftError, ValueError, IndexError, PriceNotFoundError) as error:
        await gifts.session.rollback()
        text = (
            str(error)
            if isinstance(error, GiftError)
            else "Откройте подарок и выберите тариф заново."
        )
        await bot.send_message(
            user.telegram_id,
            text,
            reply_markup=keyboard([[button("Мои подарки", "gift:list:sent:0")]]),
        )
