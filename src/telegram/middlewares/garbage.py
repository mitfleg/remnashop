from typing import Any, Awaitable, Callable, cast

from aiogram.types import Message, TelegramObject
from loguru import logger

from src.application.dto import TelegramUserDto
from src.core.constants import USER_KEY
from src.core.enums import Command, MiddlewareEventType

from .base import EventTypedMiddleware


class GarbageMiddleware(EventTypedMiddleware):
    __event_types__ = [MiddlewareEventType.MESSAGE]

    async def middleware_logic(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = cast(Message, event)
        messages = data.get("media_album", [message])
        user: TelegramUserDto = data[USER_KEY]

        for message in messages:
            if message.text != f"/{Command.START.value.command}":
                try:
                    await message.delete()
                    logger.debug(f"Message '{message.content_type}' deleted from {user.log}")
                except Exception as e:
                    logger.debug(f"Failed to delete message from {user.log}: '{e}'")

        return await handler(event, data)
