"""Native rich menus built from the existing, permission-filtered keyboards."""

from html import escape
from typing import Any

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram_dialog.utils import remove_intent_id


class SendRichMessage(TelegramMethod[Message]):
    __api_method__ = "sendRichMessage"
    __returning__ = Message

    chat_id: int
    rich_message: dict[str, Any]
    message_thread_id: int | None = None
    business_connection_id: str | None = None
    message_effect_id: str | None = None
    protect_content: bool | None = None


class EditRichMessage(TelegramMethod[Message]):
    __api_method__ = "editMessageText"
    __returning__ = Message

    chat_id: int
    message_id: int
    rich_message: dict[str, Any]
    business_connection_id: str | None = None


def _attr(name: str, value: Any) -> str:
    return f' {name}="{escape(str(value), quote=True)}"'


def button_html(button: InlineKeyboardButton) -> str:  # noqa: C901
    data = button.model_dump(exclude_none=True)
    text = escape(data.pop("text"))
    style = data.pop("style", None)
    emoji = data.pop("icon_custom_emoji_id", None)
    if emoji:
        text = f'<tg-emoji emoji-id="{escape(emoji, quote=True)}">▫️</tg-emoji> {text}'
    attrs = _attr("style", style) if style else ""
    if len(data) != 1:
        raise ValueError("Unsupported rich button: keep the original keyboard")
    kind, value = next(iter(data.items()))
    attrs = _attr("type", kind) + attrs
    if kind in {"url", "callback_data", "switch_inline_query", "switch_inline_query_current_chat"}:
        name = {"callback_data": "data", "url": "url"}.get(kind, "query")
        attrs += _attr(name, value)
    elif kind in {"web_app", "login_url"}:
        if kind == "login_url" and value.get("bot_username"):
            raise ValueError("Rich login buttons do not support another bot username")
        attrs += _attr("url", value["url"])
        if value.get("forward_text"):
            attrs += _attr("forward-text", value["forward_text"])
        if value.get("request_write_access"):
            attrs += " request-write-access"
    elif kind == "switch_inline_query_chosen_chat":
        attrs += _attr("query", value.get("query", ""))
        for field in (
            "allow_user_chats",
            "allow_bot_chats",
            "allow_group_chats",
            "allow_channel_chats",
        ):
            if value.get(field):
                attrs += " " + field.replace("_", "-")
    elif kind == "copy_text":
        attrs += _attr("text", value["text"])
    elif kind != "disabled":
        raise ValueError("Unsupported rich button: keep the original keyboard")
    return f"<tg-button{attrs}>{text}</tg-button>"


def rows_html(rows: list[list[InlineKeyboardButton]]) -> str:
    return "".join(
        '<tg-button-row align="center">'
        + "".join(button_html(button) for button in row[offset : offset + 8])
        + "</tg-button-row>"
        for row in rows
        for offset in range(0, len(row), 8)
    )


def _section(heading: str, text: str, rows: list[list[InlineKeyboardButton]]) -> str:
    if not rows:
        return ""
    return (
        f"<hr/><table bordered compact><tr><td><b>{heading}</b>{text}</td></tr></table>"
        + rows_html(rows)
    )


def menu_html(text: str, markup: InlineKeyboardMarkup, state: str) -> str:
    """Do not rebuild buttons from IDs: preserve every rendered action and its payload."""
    text = text.replace("\n", "<br>")
    if state != "MainMenu:MAIN":
        return text + "<hr/>" + rows_html(markup.inline_keyboard)

    groups: dict[str, list[list[InlineKeyboardButton]]] = {
        key: [] for key in ("access", "whitelist", "gift", "social", "cabinet", "other")
    }
    for row in markup.inline_keyboard:
        # Keep mixed/custom rows intact; classify only known first-party actions.
        ids = [remove_intent_id(button.callback_data or "")[1] for button in row]
        if all(
            value
            in {"devices", "payment_subscription", "not_available", "trial_free", "trial_paid"}
            for value in ids
        ):
            group = "access"
        elif any(value == "whitelist" for value in ids):
            group = "whitelist"
        elif any(value == "gift_new" for value in ids):
            group = "gift"
        elif any(value == "invite" for value in ids) or any("Поддержка" in b.text for b in row):
            group = "social"
        elif all("Подключиться" in button.text for button in row):
            group = "access"
        elif all("Личный кабинет" in button.text for button in row):
            group = "cabinet"
        else:
            group = "other"
        groups[group].append(row)

    profile, separator, subscription = text.partition("</blockquote>")
    if separator and "Профиль" in profile:
        profile = "<details><summary>Профиль</summary>" + profile + separator + "</details>"
        summary = subscription
    else:
        profile, summary = "", text
    return (
        profile
        + summary
        + rows_html(groups["access"])
        + _section("🛡 Белые списки", "<br>Дополнительный доступ для INCY", groups["whitelist"])
        + _section("🎁 Подарить VPN", "<br>Выберите получателя и срок подарка", groups["gift"])
        + _section("Приглашения и поддержка", "", groups["social"])
        + _section("Личный кабинет", "", groups["cabinet"])
        + _section("Дополнительно", "", groups["other"])
    )
