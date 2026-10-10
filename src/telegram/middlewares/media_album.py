import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update
from cachetools import TTLCache


@dataclass
class PendingAlbum:
    messages: dict[int, Message] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    dispatched: bool = False


class MediaAlbumMiddleware(BaseMiddleware):
    """Collect before throttling and dialog locks, then dispatch one complete input."""

    def __init__(self, quiet_period: float = 1.0) -> None:
        self.quiet_period = quiet_period
        self.pending: dict[tuple[int, int, str], PendingAlbum] = {}
        self.completed: TTLCache[tuple[int, int, str], dict[int, Message]] = TTLCache(
            maxsize=1000,
            ttl=60,
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        message = event.message
        if not message or not message.media_group_id:
            await self._wait_for_albums(event, data["bot"].id)
            return await handler(event, data)

        key = (data["bot"].id, message.chat.id, message.media_group_id)
        if key in self.pending:
            album = self.pending[key]
            if album.dispatched:
                await album.done.wait()
                return await self(handler, event, data)
            if message.message_id not in album.messages:
                album.messages[message.message_id] = message
                album.changed.set()
            return None
        previous = self.completed.get(key, {})
        if message.message_id in previous:
            return None
        album = PendingAlbum(messages={**previous, message.message_id: message})
        self.pending[key] = album
        try:
            while True:
                album.changed.clear()
                try:
                    await asyncio.wait_for(album.changed.wait(), timeout=self.quiet_period)
                except TimeoutError:
                    break
            messages = sorted(album.messages.values(), key=lambda item: item.message_id)
            album.dispatched = True
            data["media_album"] = messages
            # Late updates are merged with the earlier group, never saved as one bare photo.
            first = next((item for item in messages if item.caption), messages[0])
            result = await handler(event.model_copy(update={"message": first}), data)
            self.completed[key] = dict(album.messages)
            return result
        finally:
            self.pending.pop(key, None)
            album.done.set()

    async def _wait_for_albums(self, event: Update, bot_id: int) -> None:
        message = event.message
        # A quick Preview/Send click must not overtake the album being collected.
        chat = (
            message.chat
            if message
            else (
                event.callback_query.message.chat
                if event.callback_query and event.callback_query.message
                else None
            )
        )
        if chat:
            waiting = [p.done.wait() for k, p in self.pending.items() if k[:2] == (bot_id, chat.id)]
            if waiting:
                await asyncio.gather(*waiting)
