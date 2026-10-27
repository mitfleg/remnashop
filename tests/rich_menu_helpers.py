"""Decode native buttons as a Telegram client would when producing a callback."""

import re
from html import unescape
from html.parser import HTMLParser

from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message


class RichButtons(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.buttons = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "tg-button":
            self.current = {"attrs": dict(attrs), "text": ""}
        elif tag == "tg-emoji" and self.current:
            self.current["emoji"] = dict(attrs)["emoji-id"]

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag != "tg-button" or self.current is None:
            return
        attrs = self.current["attrs"]
        kind = attrs["type"]
        if kind in {"url", "callback_data"}:
            value = attrs["url" if kind == "url" else "data"]
        elif kind in {"switch_inline_query", "switch_inline_query_current_chat"}:
            value = attrs["query"]
        elif kind in {"web_app", "login_url"}:
            value = {"url": attrs["url"]}
        elif kind == "copy_text":
            value = {"text": attrs["text"]}
        elif kind == "switch_inline_query_chosen_chat":
            value = {"query": attrs["query"]}
            value.update(
                {
                    field: field.replace("_", "-") in attrs
                    for field in (
                        "allow_user_chats",
                        "allow_bot_chats",
                        "allow_group_chats",
                        "allow_channel_chats",
                    )
                }
            )
        else:
            value = {}
        button = {"text": self.current["text"], kind: value}
        if attrs.get("style"):
            button["style"] = attrs["style"]
        self.buttons.append(InlineKeyboardButton(**button))
        self.current = None


def rich_response(method, message_id=10):
    html = method.rich_message["html"]
    buttons = RichButtons(html).buttons
    # Retain legacy text in existing flow assertions; callbacks are decoded from
    # the actual rich payload, never from a duplicate legacy keyboard.
    text = re.sub(r"^<img[^>]*>", "", html.split("<hr/>", 1)[0]).replace("<br>", "\n")
    return Message(
        message_id=getattr(method, "message_id", None) or message_id,
        date=0,
        chat=Chat(id=123, type="private"),
        text=unescape(text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons]),
        rich_message=method.rich_message,
        has_protected_content=getattr(method, "protect_content", None),
    )
