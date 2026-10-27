from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramNotFound
from aiogram.types import (
    Chat,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram_dialog import Window as OriginalWindow
from aiogram_dialog.api.entities import MediaAttachment, ShowMode
from aiogram_dialog.widgets.text import Const
from rich_menu_helpers import RichButtons, rich_response

from src.core.constants import CONFIG_KEY, USER_KEY
from src.telegram.message_manager import MessageManager
from src.telegram.rich_messages import SendRichMessage, button_html, menu_html
from src.telegram.states import MainMenu
from src.telegram.window import NewMessageWithEffect, Window


@pytest.mark.parametrize(
    "owner_only,viewer,enabled,expected",
    [
        (True, 123, True, True),
        (True, 456, True, False),
        (False, 456, True, True),
        (False, 123, False, False),
        (True, None, True, False),
    ],
)
async def test_owner_canary_preserves_legacy_menu_for_other_users(
    owner_only, viewer, enabled, expected
):
    window = Window(Const("Подписка"), state=MainMenu.MAIN)
    original = message()
    manager = SimpleNamespace(
        middleware_data={
            CONFIG_KEY: SimpleNamespace(
                bot=SimpleNamespace(
                    use_rich_messages=enabled,
                    rich_messages_owner_only=owner_only,
                    owner_id=123,
                )
            ),
            USER_KEY: SimpleNamespace(telegram_id=viewer),
        }
    )
    with patch.object(OriginalWindow, "render", AsyncMock(return_value=original)):
        result = await window.render(SimpleNamespace(), manager)
    assert bool(result.rich_message_html) is expected
    assert result.reply_markup == original.reply_markup and result.text == original.text


@pytest.mark.parametrize(
    "action",
    [
        {"callback_data": "context\x1dpayment_renew"},
        {"url": "https://example.test/?a=1&b=2"},
        {"web_app": {"url": "https://example.test/app?a=1&b=2"}},
        {"login_url": {"url": "https://example.test/login"}},
        {"switch_inline_query": ""},
        {"switch_inline_query_current_chat": "gift:42"},
        {
            "switch_inline_query_chosen_chat": {
                "query": "invite",
                "allow_user_chats": True,
                "allow_group_chats": True,
                "allow_channel_chats": False,
            }
        },
        {"copy_text": {"text": 'key "quoted" & <value>'}},
    ],
)
def test_rich_buttons_keep_every_action_payload_and_style(action):
    original = InlineKeyboardButton(text='VPN <&> "test"', style="primary", **action)
    decoded = RichButtons(button_html(original)).buttons[0]
    assert decoded.text == original.text
    for key, value in action.items():
        if key == "switch_inline_query_chosen_chat":
            actual, expected = getattr(decoded, key), getattr(original, key)
            assert actual.query == expected.query
            for flag in (
                "allow_user_chats",
                "allow_bot_chats",
                "allow_group_chats",
                "allow_channel_chats",
            ):
                assert bool(getattr(actual, flag)) == bool(getattr(expected, flag))
        else:
            assert getattr(decoded, key) == getattr(original, key)
    assert decoded.style == "primary"


def test_main_menu_keeps_custom_admin_reserved_and_first_party_buttons_once():
    buttons = [
        InlineKeyboardButton(text="Подключиться", url="https://connect.test"),
        InlineKeyboardButton(text="Подключиться (резерв)", url="https://reserve.test"),
        InlineKeyboardButton(text="Подписка", callback_data="ctx\x1dpayment_subscription"),
        InlineKeyboardButton(text="Устройства", callback_data="ctx\x1ddevices"),
        InlineKeyboardButton(text="Белые списки · 249 ₽", callback_data="ctx\x1dwhitelist"),
        InlineKeyboardButton(text="Подарить VPN", callback_data="ctx\x1dgift_new"),
        InlineKeyboardButton(
            text="Пригласить",
            switch_inline_query_chosen_chat={"query": "invite", "allow_user_chats": True},
        ),
        InlineKeyboardButton(text="Поддержка", url="https://support.test"),
        InlineKeyboardButton(text="Личный кабинет", web_app={"url": "https://cabinet.test"}),
        InlineKeyboardButton(text="Канал", url="https://channel.test"),
        InlineKeyboardButton(text="Панель управления", callback_data="ctx\x1ddashboard"),
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])
    text = "<b>Профиль</b><blockquote>ID: 123</blockquote><b>Подписка</b>: до завтра"
    html = menu_html(text, markup, "MainMenu:MAIN")
    decoded = RichButtons(html).buttons
    assert len(decoded) == len(buttons)
    for original in buttons:
        actual = next(button for button in decoded if button.text == original.text)
        if original.switch_inline_query_chosen_chat:
            assert actual.switch_inline_query_chosen_chat.query == "invite"
            assert actual.switch_inline_query_chosen_chat.allow_user_chats
        else:
            assert original == actual
    assert "до завтра" in html and "ID: 123" in html
    assert "<details>" in html and "<table bordered compact><tr><td><b>🎁" in html
    assert markup.inline_keyboard == [[button] for button in buttons]


def test_unsupported_game_or_pay_action_is_never_silently_dropped():
    with pytest.raises(ValueError):
        button_html(InlineKeyboardButton(text="Оплатить", pay=True))
    with pytest.raises(ValueError):
        button_html(InlineKeyboardButton(text="Игра", callback_game={}))


def test_attributes_cannot_inject_buttons_or_change_callback_data():
    payload = '"><tg-button type="callback_data" data="admin">'
    html = button_html(InlineKeyboardButton(text=payload, callback_data=payload))
    decoded = RichButtons(html).buttons
    assert len(decoded) == 1 and decoded[0].callback_data == payload


class FakeBot:
    id = 123456

    def __init__(self, reject=None):
        self.calls = []
        self.reject = reject
        self.send_message = AsyncMock(
            return_value=Message(
                message_id=10, date=0, chat=Chat(id=123, type="private"), text="fallback"
            )
        )
        self.edit_message_text = AsyncMock(return_value=self.send_message.return_value)
        self.send_photo = AsyncMock(return_value=self.send_message.return_value)
        self.delete_message = AsyncMock(return_value=True)

    async def __call__(self, method):
        self.calls.append(method)
        if self.reject:
            raise self.reject(method=method, message="rich format unavailable")
        return rich_response(method)


def message(banner=False):
    return NewMessageWithEffect(
        chat=Chat(id=123, type="private"),
        text="Существующий текст",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Далее", callback_data="ctx\x1dnext")]]
        ),
        rich_message_html="<b>Существующий текст</b><tg-button-row>"
        '<tg-button type="callback_data" data="ctx&#29;next">Далее</tg-button>'
        "</tg-button-row>",
        media=MediaAttachment(type=ContentType.PHOTO, path="assets/banners/ru/menu.png")
        if banner
        else None,
        protect_content=True,
    )


@pytest.mark.parametrize("banner", [False, True])
async def test_native_menu_edits_one_message_and_embeds_banner(banner):
    manager, bot = MessageManager(), FakeBot()
    new = message(banner)
    old = await manager.show_message(bot, new, None)
    second = await manager.show_message(bot, new, old)
    assert [method.__api_method__ for method in bot.calls] == ["sendRichMessage", "editMessageText"]
    assert old.message_id == second.message_id == 10
    assert old.content_type == ContentType.UNKNOWN
    assert bot.calls[0].protect_content is True
    if banner:
        assert "tg://photo?id=menu_banner" in bot.calls[0].rich_message["html"]
        assert isinstance(bot.calls[0].rich_message["media"][0]["media"].media, FSInputFile)
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert new.media is not None if banner else new.media is None


@pytest.mark.parametrize("error", [TelegramBadRequest, TelegramNotFound])
@pytest.mark.parametrize("banner", [False, True])
async def test_rejected_rich_format_keeps_original_media_text_and_keyboard(error, banner):
    manager, bot, new = MessageManager(), FakeBot(reject=error), message(banner)
    await manager.show_message(bot, new, None)
    await manager.show_message(bot, new, None)
    assert len(bot.calls) == 1  # Do not retry unsupported formatting on every click.
    method = bot.send_photo if banner else bot.send_message
    assert method.await_count == 2
    assert method.call_args.kwargs["reply_markup"] == new.reply_markup
    assert method.call_args.kwargs["caption" if banner else "text"] == new.text
    assert method.call_args.kwargs["protect_content"] is True


async def test_network_uncertainty_does_not_send_a_duplicate_legacy_message():
    manager, bot = MessageManager(), FakeBot(reject=TelegramNetworkError)
    with pytest.raises(TelegramNetworkError):
        await manager.show_message(bot, message(), None)
    bot.send_message.assert_not_awaited()


async def test_old_embedded_buttons_are_removed_when_another_message_is_sent():
    manager, bot = MessageManager(), FakeBot()
    old = await manager.show_message(bot, message(), None)
    await manager.show_message(
        bot,
        NewMessageWithEffect(chat=old.chat, text="Другое сообщение", show_mode=ShowMode.SEND),
        old,
    )
    assert bot.edit_message_text.call_args.kwargs["text"] == "Существующий текст"
    assert not bot.edit_message_text.call_args.kwargs.get("reply_markup")


async def test_raw_request_serializes_nested_upload_without_exposing_other_fields():
    bot = Bot("123456:TEST_TOKEN")
    payload = await MessageManager()._rich_payload(bot, message(banner=True))
    method = SendRichMessage(chat_id=123, rich_message=payload)
    files = {}
    data = bot.session.prepare_value(method.rich_message, bot=bot, files=files)
    assert '"html"' in data and "attach://" in data and len(files) == 1
    await bot.session.close()
