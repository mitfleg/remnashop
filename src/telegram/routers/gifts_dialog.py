"""Gift windows use the same dialog, banner and keyboard widgets as subscriptions."""

from aiogram.enums import ButtonStyle
from aiogram.fsm.state import State
from aiogram_dialog import Dialog
from aiogram_dialog.widgets.kbd import Keyboard
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Format
from magic_filter import F

from src.core.enums import BannerName, PaymentGatewayType
from src.telegram.states import Gifts
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Button, Column, Group, ListGroup, Row, Select, SwitchTo, Url
from src.telegram.window import Window

from .gifts import (
    gift_getter,
    on_choose_days,
    on_dialog_start,
    on_duration_select,
    on_gift_history,
    on_gift_start,
    on_history_page,
    on_history_tab,
    on_main_menu,
    on_open_card,
    on_payment_select,
    on_refresh,
)


def window(state: State, *buttons: Keyboard) -> Window:
    return Window(
        Banner(BannerName.SUBSCRIPTION),
        Format("{text}"),
        *buttons,
        *(
            [Row(Button(I18nFormat("btn-gift.history"), id="history", on_click=on_gift_history))]
            if state != Gifts.HISTORY
            else []
        ),
        Row(Button(I18nFormat("btn-back.menu"), id="main", on_click=on_main_menu)),
        IgnoreUpdate(),
        state=state,
        getter=gift_getter,
    )


router = Dialog(
    window(
        Gifts.PICKER,
        Row(Button(I18nFormat("btn-gift.recipient"), id="picker", on_click=on_gift_start)),
    ),
    window(
        Gifts.RECIPIENT,
        Row(Button(I18nFormat("btn-gift.days"), id="days", on_click=on_choose_days, when="valid")),
        Row(Button(I18nFormat("btn-gift.change-recipient"), id="change", on_click=on_gift_start)),
    ),
    window(
        Gifts.DURATION,
        Group(
            Select(
                I18nFormat(
                    "btn-gift.duration",
                    period=F["item"]["period"],
                    final_amount=F["item"]["final_amount"],
                    currency=F["item"]["currency"],
                ),
                id="duration",
                item_id_getter=lambda item: item["days"],
                items="durations",
                type_factory=int,
                on_click=on_duration_select,
            ),
            width=2,
        ),
        Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=Gifts.RECIPIENT)),
    ),
    window(
        Gifts.PAYMENT_METHOD,
        Column(
            Select(
                I18nFormat(
                    "btn-subscription.payment-method",
                    gateway_title=F["item"]["gateway_title"],
                    final_amount=F["item"]["final_amount"],
                    original_amount=F["item"]["original_amount"],
                    discount_percent=F["item"]["discount_percent"],
                    currency=F["item"]["currency"],
                ),
                id="payment",
                item_id_getter=lambda item: item["gateway_type"],
                items="payment_methods",
                type_factory=PaymentGatewayType,
                on_click=on_payment_select,
            ),
        ),
        Row(
            SwitchTo(I18nFormat("btn-subscription.back-duration"), id="back", state=Gifts.DURATION)
        ),
    ),
    window(
        Gifts.CARD,
        ListGroup(
            Url(Format("{item[text]}"), Format("{item[url]}"), style=Style(ButtonStyle.SUCCESS)),
            id="links",
            item_id_getter=lambda item: item["id"],
            items="links",
        ),
        Row(
            Button(
                I18nFormat("btn-gift.days"),
                id="days",
                on_click=on_choose_days,
                when=F["valid"] & F["draft"],
            )
        ),
        Row(
            Button(I18nFormat("btn-gift.refresh"), id="refresh", on_click=on_refresh, when="valid")
        ),
    ),
    window(
        Gifts.HISTORY,
        Row(
            Button(I18nFormat("btn-gift.sent"), id="sent", on_click=on_history_tab),
            Button(I18nFormat("btn-gift.received"), id="received", on_click=on_history_tab),
        ),
        Column(
            Select(
                Format("{item[label]}"),
                id="open",
                items="items",
                item_id_getter=lambda item: item["id"],
                type_factory=int,
                on_click=on_open_card,
            )
        ),
        Row(
            Button(
                I18nFormat("btn-common.prev"),
                id="previous",
                on_click=on_history_page,
                when="previous_page",
            ),
            Button(
                I18nFormat("btn-common.next"), id="next", on_click=on_history_page, when="next_page"
            ),
        ),
        Row(Button(I18nFormat("btn-gift.new"), id="new", on_click=on_gift_start)),
    ),
    on_start=on_dialog_start,
)
