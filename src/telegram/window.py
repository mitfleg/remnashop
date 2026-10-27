from dataclasses import dataclass
from typing import Optional

from aiogram.client.default import Default
from aiogram.types import InlineKeyboardMarkup
from aiogram_dialog import DialogManager, DialogProtocol
from aiogram_dialog import Window as WindowProtocol
from aiogram_dialog.api.entities import NewMessage

from src.core.constants import CONFIG_KEY, USER_KEY
from src.telegram.rich_messages import menu_html


@dataclass
class NewMessageWithEffect(NewMessage):
    message_effect_id: Optional[str] = None
    rich_message_html: Optional[str] = None
    legacy_message: Optional[NewMessage] = None
    rich_used: bool = False


class Window(WindowProtocol):
    async def render(self, dialog: DialogProtocol, manager: DialogManager) -> NewMessage:
        new_message = await super().render(dialog, manager)
        config = manager.middleware_data.get(CONFIG_KEY)
        state = self.get_state().state or ""
        rich_html = None
        owner_only = getattr(getattr(config, "bot", None), "rich_messages_owner_only", False)
        viewer = manager.middleware_data.get(USER_KEY)
        owner = getattr(getattr(config, "bot", None), "owner_id", None)
        parse_mode = new_message.parse_mode
        if isinstance(parse_mode, Default):
            bot = manager.middleware_data.get("bot")
            parse_mode = getattr(getattr(bot, "default", None), "parse_mode", None)
        if (
            getattr(getattr(config, "bot", None), "use_rich_messages", False)
            and (
                not owner_only
                or (owner is not None and getattr(viewer, "telegram_id", None) == owner)
            )
            and state.split(":", 1)[0] in {"MainMenu", "Subscription", "Gifts", "Whitelist"}
            and isinstance(new_message.reply_markup, InlineKeyboardMarkup)
            and parse_mode in (None, "HTML")
        ):
            try:
                rich_html = menu_html(new_message.text or "", new_message.reply_markup, state)
            except ValueError:
                # Unsupported action types retain the complete original menu.
                rich_html = None

        return NewMessageWithEffect(
            chat=new_message.chat,
            thread_id=new_message.thread_id,
            business_connection_id=new_message.business_connection_id,
            text=new_message.text,
            reply_markup=new_message.reply_markup,
            parse_mode=new_message.parse_mode,
            protect_content=new_message.protect_content,
            show_mode=new_message.show_mode,
            media=new_message.media,
            link_preview_options=new_message.link_preview_options,
            message_effect_id=getattr(manager, "message_effect_id", None),
            rich_message_html=rich_html,
        )
