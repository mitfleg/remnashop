from dataclasses import replace
from typing import Any, cast

from aiogram import Bot
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import Message
from aiogram_dialog.api.entities import NewMessage, OldMessage
from aiogram_dialog.manager.message_manager import INPUT_MEDIA_TYPES, SEND_METHODS
from aiogram_dialog.manager.message_manager import MessageManager as MessageManagerProtocol
from loguru import logger

from src.telegram.rich_messages import EditRichMessage, SendRichMessage
from src.telegram.window import NewMessageWithEffect


class MessageManager(MessageManagerProtocol):
    def __init__(self) -> None:
        self._rich_disabled: set[int] = set()

    async def show_message(
        self, bot: Bot, new_message: NewMessage, old_message: OldMessage | None
    ) -> OldMessage:
        rich = getattr(new_message, "rich_message_html", None)
        supported_media = new_message.media is None or new_message.media.type in {
            ContentType.PHOTO,
            ContentType.ANIMATION,
            ContentType.VIDEO,
            ContentType.AUDIO,
            ContentType.DOCUMENT,
        }
        if not rich or bot.id in self._rich_disabled or not supported_media:
            return await super().show_message(bot, new_message, old_message)
        # Rich messages embed the banner. The dialog manager must treat them as
        # text messages so subsequent steps edit the same message, not its caption.
        wire = replace(cast(NewMessageWithEffect, new_message), media=None)
        wire.legacy_message = new_message
        wire.rich_used = False
        result = await super().show_message(bot, wire, old_message)
        if wire.rich_used:
            result.text = new_message.text
            result.content_type = ContentType.UNKNOWN
        return result

    def _can_edit(self, new_message: NewMessage, old_message: OldMessage) -> bool:
        if old_message.content_type == ContentType.UNKNOWN and new_message.media:
            return False
        return super()._can_edit(new_message, old_message)

    async def _rich_payload(self, bot: Bot, message: NewMessage) -> dict[str, Any]:
        html = getattr(message, "rich_message_html", "")
        original = getattr(message, "legacy_message", None) or message
        payload: dict[str, Any] = {"html": html}
        if original.media:
            attachment = original.media
            media = INPUT_MEDIA_TYPES[attachment.type](
                media=await self.get_media_source(attachment, bot), **attachment.kwargs
            )
            kind = {
                ContentType.PHOTO: "photo",
                ContentType.ANIMATION: "video",
                ContentType.VIDEO: "video",
                ContentType.AUDIO: "audio",
                ContentType.DOCUMENT: "document",
            }[attachment.type]
            source = f"tg://{kind}?id=menu_banner"
            tag = "img" if kind == "photo" else "tg-document" if kind == "document" else kind
            media_html = (
                f'<img src="{source}"/>' if tag == "img" else (f'<{tag} src="{source}"></{tag}>')
            )
            payload["html"] = media_html + html
            payload["media"] = [{"id": "menu_banner", "media": media}]
        return payload

    def _disable_rich(self, bot: Bot) -> None:
        self._rich_disabled.add(bot.id)
        logger.warning("Rich menu rejected; retaining the complete original menu")

    async def send_text(self, bot: Bot, new_message: NewMessage) -> Message:
        if getattr(new_message, "rich_message_html", None) and bot.id not in self._rich_disabled:
            try:
                result = await bot(
                    SendRichMessage(
                        chat_id=new_message.chat.id,
                        rich_message=await self._rich_payload(bot, new_message),
                        message_thread_id=new_message.thread_id,
                        business_connection_id=new_message.business_connection_id,
                        message_effect_id=getattr(new_message, "message_effect_id", None),
                        protect_content=new_message.protect_content,
                    )
                )
                cast(NewMessageWithEffect, new_message).rich_used = True
                return result
            except (TelegramBadRequest, TelegramNotFound):
                self._disable_rich(bot)
        original = getattr(new_message, "legacy_message", None) or new_message
        if original.media:
            return await self.send_media(bot, original)
        return await self._send_plain_text(bot, original)

    async def _send_plain_text(self, bot: Bot, new_message: NewMessage) -> Message:
        return await bot.send_message(
            new_message.chat.id,
            text=new_message.text,  # type: ignore[arg-type]
            message_thread_id=new_message.thread_id,
            business_connection_id=new_message.business_connection_id,
            reply_markup=new_message.reply_markup,
            parse_mode=new_message.parse_mode,
            protect_content=new_message.protect_content,
            link_preview_options=new_message.link_preview_options,
            message_effect_id=getattr(new_message, "message_effect_id", None),
        )

    async def edit_text(
        self, bot: Bot, new_message: NewMessage, old_message: OldMessage
    ) -> Message:
        if getattr(new_message, "rich_message_html", None) and bot.id not in self._rich_disabled:
            try:
                result = await bot(
                    EditRichMessage(
                        chat_id=old_message.chat.id,
                        message_id=old_message.message_id,
                        rich_message=await self._rich_payload(bot, new_message),
                        business_connection_id=new_message.business_connection_id,
                    )
                )
                cast(NewMessageWithEffect, new_message).rich_used = True
                return result
            except TelegramBadRequest as exc:
                if any(
                    value in exc.message.lower()
                    for value in (
                        "message is not modified",
                        "message can't be edited",
                        "message to edit not found",
                    )
                ):
                    raise
                self._disable_rich(bot)
            except TelegramNotFound:
                self._disable_rich(bot)
        original = getattr(new_message, "legacy_message", None) or new_message
        if original.media:
            await self.remove_message_safe(bot, old_message, None)
            return await self.send_media(bot, original)
        return await super().edit_text(bot, original, old_message)

    async def remove_inline_kbd(self, bot: Bot, old_message: OldMessage | None) -> Message | None:
        if old_message and old_message.content_type == ContentType.UNKNOWN:
            # Removing reply_markup alone does not remove buttons inside rich text.
            try:
                result = await bot.edit_message_text(
                    chat_id=old_message.chat.id,
                    message_id=old_message.message_id,
                    business_connection_id=old_message.business_connection_id,
                    text=old_message.text if isinstance(old_message.text, str) else "Меню закрыто",
                    parse_mode="HTML",
                )
                return result if isinstance(result, Message) else None
            except TelegramBadRequest as exc:
                if any(
                    value in exc.message.lower()
                    for value in (
                        "message is not modified",
                        "message can't be edited",
                        "message to edit not found",
                        "message_id_invalid",
                    )
                ):
                    return None
                raise
        return await super().remove_inline_kbd(bot, old_message)

    async def send_media(self, bot: Bot, new_message: NewMessage) -> Message:
        method = getattr(bot, SEND_METHODS[new_message.media.type], None)  # type: ignore[union-attr]

        if not method:
            raise ValueError(
                f"ContentType {new_message.media.type} is not supported",  # type: ignore[union-attr]
            )

        return await method(  # type: ignore[no-any-return]
            new_message.chat.id,
            await self.get_media_source(new_message.media, bot),  # type: ignore[arg-type]
            message_thread_id=new_message.thread_id,
            business_connection_id=new_message.business_connection_id,
            caption=new_message.text,
            reply_markup=new_message.reply_markup,
            parse_mode=new_message.parse_mode,
            protect_content=new_message.protect_content,
            message_effect_id=getattr(new_message, "message_effect_id", None),
            **new_message.media.kwargs,  # type: ignore[union-attr]
        )
