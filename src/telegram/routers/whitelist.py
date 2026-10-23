from html import escape
from typing import Any
from uuid import uuid4

from aiogram import F as TelegramF
from aiogram import Router
from aiogram.enums import ButtonStyle
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.kbd import Button as DialogButton
from aiogram_dialog.widgets.kbd import Select as DialogSelect
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Const, Format
from dishka import FromDishka
from dishka.integrations.aiogram import inject
from dishka.integrations.aiogram_dialog import inject as inject_dialog
from magic_filter import F

from src.application.dto import TelegramUserDto
from src.core.constants import USER_KEY
from src.core.enums import BannerName
from src.infrastructure.services.cabinet_whitelist import CabinetError, CabinetWhitelist
from src.telegram.states import MainMenu, Whitelist
from src.telegram.widgets import Banner, IgnoreUpdate
from src.telegram.widgets.kbd import Button, Column, Row, Select, Url
from src.telegram.window import Window

router = Router(name=__name__)


def rubles(kopecks: int) -> str:
    return f"{kopecks / 100:,.2f}".replace(",", " ").replace(".00", "").replace(".", ",") + " ₽"


async def open_whitelist(manager: DialogManager, cabinet: CabinetWhitelist) -> None:
    user = manager.middleware_data[USER_KEY]
    try:
        offer = await cabinet.offer(user)
        error = ""
    except CabinetError as exc:
        offer, error = {}, str(exc)
    await manager.start(
        Whitelist.MAIN,
        mode=StartMode.RESET_STACK,
        data={"offer": offer, "error": error, "checkout_key": str(uuid4())},
        show_mode=ShowMode.EDIT,
    )


@inject_dialog
async def on_open(
    callback: CallbackQuery,
    widget: DialogButton,
    dialog_manager: DialogManager,
    cabinet: FromDishka[CabinetWhitelist],
) -> None:
    await callback.answer()
    await open_whitelist(dialog_manager, cabinet)


@router.message(CommandStart(deep_link=True), TelegramF.text.regexp(r"^/start(?:@\w+)? whitelist$"))
@inject
async def on_return(
    message: Message,
    dialog_manager: DialogManager,
    cabinet: FromDishka[CabinetWhitelist],
) -> None:
    await open_whitelist(dialog_manager, cabinet)


async def on_start(start_data: dict[str, Any], manager: DialogManager) -> None:
    manager.dialog_data.update(start_data)


@inject_dialog
async def on_provider(
    callback: CallbackQuery,
    widget: DialogSelect[Any],
    dialog_manager: DialogManager,
    item_id: str,
    cabinet: FromDishka[CabinetWhitelist],
) -> None:
    await callback.answer()
    data = dialog_manager.dialog_data
    user = dialog_manager.middleware_data[USER_KEY]
    try:
        invoice = await cabinet.checkout(user, data["offer"], item_id, data["checkout_key"])
        data.update(invoice=invoice, error="")
    except CabinetError as exc:
        data["error"] = str(exc)


@inject_dialog
async def on_refresh(
    callback: CallbackQuery,
    widget: DialogButton,
    dialog_manager: DialogManager,
    cabinet: FromDishka[CabinetWhitelist],
) -> None:
    await callback.answer()
    data = dialog_manager.dialog_data
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    try:
        if invoice := data.get("invoice"):
            result = await cabinet.request(user, "payment", paymentId=invoice["localPaymentId"])
            data["payment"] = result
        else:
            data["offer"] = await cabinet.offer(user)
        data["error"] = ""
    except CabinetError as exc:
        data["error"] = str(exc)


async def on_menu(
    callback: CallbackQuery, widget: DialogButton, dialog_manager: DialogManager
) -> None:
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK, show_mode=ShowMode.EDIT)


async def getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    data = dialog_manager.dialog_data
    offer = data.get("offer", {})
    invoice = data.get("invoice", {})
    payment = data.get("payment", {})
    status = payment.get("status", invoice.get("status"))
    text = "<b>🛡 Белые списки</b>\n\n"
    if payment.get("provisioned"):
        text += (
            "Оплата получена. Дни сохранены и начнут использоваться после возобновления VPN."
            if payment.get("paused")
            else "Белые списки подключены. Обновите подписку в приложении INCY."
        )
    elif status in ("CANCELED", "REFUNDED"):
        text += "Платёж отменён или возвращён. Для новой покупки вернитесь в главное меню."
    elif status == "SUCCEEDED":
        text += "Оплата получена. Подключаем белые списки. Повторно платить не нужно."
    elif invoice:
        text += (
            f"К оплате: <b>{rubles(invoice.get('amountKopecks') or offer['priceKopecks'])}</b>\n"
            "Нажмите «Оплатить», затем «Проверить оплату». "
            "После оплаты доступ включится автоматически."
        )
    elif offer.get("available"):
        text += (
            f"<b>{rubles(offer['priceKopecks'])} · {offer['durationDays']} дней</b>\n\n"
            "Дополнительные серверы для приложения <b>INCY</b>.\n"
            "Основной тариф и ключ сохраняются. Если VPN-подписка закончится раньше, "
            "оставшиеся дни белых списков будут на паузе до её продления.\n\n"
            "Выберите способ оплаты:"
        )
    else:
        text += escape(offer.get("reason") or "Не удалось проверить доступность покупки.")
    if data.get("error"):
        text += "\n\n" + escape(data["error"])
    return {
        "text": text,
        "providers": offer.get("providers", []),
        "can_buy": bool(offer.get("available") and not invoice),
        "can_pay": bool(
            invoice.get("confirmationUrl") and status not in ("SUCCEEDED", "CANCELED", "REFUNDED")
        ),
        "payment_url": invoice.get("confirmationUrl") or "https://t.me/",
        "has_invoice": bool(invoice),
    }


dialog = Dialog(
    Window(
        Banner(BannerName.SUBSCRIPTION),
        Format("{text}"),
        Column(
            Select(
                Format("{item[label]}"),
                id="provider",
                items="providers",
                item_id_getter=lambda item: item["id"],
                on_click=on_provider,
                when="can_buy",
            )
        ),
        Row(
            Url(
                Const("Оплатить"),
                Format("{payment_url}"),
                when="can_pay",
                style=Style(ButtonStyle.PRIMARY),
            )
        ),
        Row(
            Button(
                Const("Проверить оплату"),
                id="check",
                on_click=on_refresh,
                when="has_invoice",
            )
        ),
        Row(
            Button(
                Const("Обновить"),
                id="refresh",
                on_click=on_refresh,
                when=~F["has_invoice"],
            )
        ),
        Row(Button(Const("В главное меню"), id="menu", on_click=on_menu)),
        IgnoreUpdate(),
        state=Whitelist.MAIN,
        getter=getter,
    ),
    on_start=on_start,
)
