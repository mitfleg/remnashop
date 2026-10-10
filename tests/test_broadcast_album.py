import asyncio
import io
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
)
from aiogram_dialog import BgManagerFactory
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.application.common.notifier import PartialBroadcastDeliveryError
from src.application.dto import (
    AlbumMediaDto,
    BroadcastDto,
    BroadcastMessageDto,
    MediaDescriptorDto,
    MessagePayloadDto,
    TempUserDto,
)
from src.core.config import AppConfig
from src.core.constants import CONTAINER_KEY, USER_KEY
from src.core.enums import BroadcastAudience, BroadcastMessageStatus, BroadcastStatus, MediaType

# Taskiq creates its broker at import time. Use a local dummy URL, never production settings.
with patch.object(
    AppConfig,
    "get",
    return_value=SimpleNamespace(redis=SimpleNamespace(dsn="redis://localhost/15")),
):
    from src.infrastructure.di.ioc import create_aiogram_container, create_taskiq_container
    from src.infrastructure.di.providers.retort import RetortProvider
    from src.infrastructure.services.notification import NotificationService
    from src.infrastructure.taskiq.tasks import broadcast as worker
    from src.telegram.middlewares.media_album import MediaAlbumMiddleware
    from src.telegram.middlewares.throttling import ThrottlingMiddleware
    from src.telegram.routers.dashboard.broadcast.handlers import on_content_input


def photo(mid=1, caption=None, entities=None, group="album"):
    return Message(
        message_id=mid,
        date=0,
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        photo=[
            PhotoSize(file_id=f"photo-{mid}", file_unique_id=f"unique-{mid}", width=640, height=480)
        ],
        media_group_id=group,
        caption=caption,
        caption_entities=entities,
    )


def update(message):
    return Update(update_id=message.message_id, message=message)


@pytest.fixture
def retort():
    return RetortProvider().get_retort()


@pytest.fixture
def composer():
    return SimpleNamespace(
        middleware_data={USER_KEY: SimpleNamespace(telegram_id=42, log="test-user")},
        dialog_data={},
        show_mode=None,
    )


async def save(messages, composer, retort, notifier=None):
    notifier = notifier or SimpleNamespace(notify_user=AsyncMock())
    composer.middleware_data["media_album"] = messages
    await on_content_input.__dishka_orig_func__(messages[0], None, composer, retort, notifier)
    return notifier


def payload(composer, retort):
    return retort.load(composer.dialog_data["payload"], MessagePayloadDto)


async def test_formatted_caption_with_custom_emoji_and_three_photos(composer, retort):
    text = "⚡" * 10 + "Продаём роутер. " * 40
    entities = [MessageEntity(type="bold", offset=10, length=20)] + [
        MessageEntity(
            type="custom_emoji", offset=i, length=1, custom_emoji_id="5368324170671202286"
        )
        for i in range(10)
    ]
    first = photo(caption=text, entities=entities)
    assert len(first.html_text) > 1024 > len(text.encode("utf-16-le")) // 2
    notifier = await save([first, photo(2), photo(3)], composer, retort)
    saved = payload(composer, retort)
    assert saved.i18n_kwargs["content"] == first.html_text
    assert "<tg-emoji" in saved.i18n_kwargs["content"]
    assert [m.media.value for m in saved.album] == ["photo-1", "photo-2", "photo-3"]
    assert saved.media is None and saved.media_type is None
    notifier.notify_user.assert_awaited_once()
    assert notifier.notify_user.call_args.kwargs["i18n_key"] == "ntf-broadcast.content-saved"


@pytest.mark.parametrize("length,accepted", [(1024, True), (1025, False)])
async def test_caption_boundary_preserves_previous_draft_on_error(
    length, accepted, composer, retort
):
    await save([photo(caption="previous")], composer, retort)
    before = deepcopy(composer.dialog_data)
    notifier = await save([photo(caption="a" * length), photo(2), photo(3)], composer, retort)
    if accepted:
        assert len(payload(composer, retort).album) == 3
    else:
        assert composer.dialog_data == before
        assert notifier.notify_user.call_args.args[1].i18n_key == "ntf-broadcast.text-too-long"


async def test_literal_html_is_not_unescaped(composer, retort):
    await save([photo(caption="Цена < 12000 & 2 > 1"), photo(2)], composer, retort)
    assert payload(composer, retort).i18n_kwargs["content"] == "Цена &lt; 12000 &amp; 2 &gt; 1"


async def test_single_photo_and_text_clear_previous_album(composer, retort):
    await save([photo(1), photo(2)], composer, retort)
    await save([photo(3, caption="single", group=None)], composer, retort)
    saved = payload(composer, retort)
    assert saved.album == [] and saved.media.value == "photo-3"
    message = Message(message_id=4, date=0, chat=Chat(id=42, type="private"), text="text")
    await save([message], composer, retort)
    saved = payload(composer, retort)
    assert saved.album == [] and saved.media is None and saved.is_text


async def test_multiple_captions_leave_existing_draft(composer, retort):
    await save([photo(caption="previous")], composer, retort)
    before = deepcopy(composer.dialog_data)
    notifier = await save([photo(caption="first"), photo(2, caption="second")], composer, retort)
    assert composer.dialog_data == before
    assert notifier.notify_user.call_args.kwargs["i18n_key"] == "ntf-broadcast.album-invalid"


async def test_caption_can_be_on_later_photo_and_empty_album_is_valid(composer, retort):
    await save([photo(), photo(2, caption="later"), photo(3)], composer, retort)
    assert payload(composer, retort).i18n_kwargs["content"] == "later"
    await save([photo(), photo(2)], composer, retort)
    assert len(payload(composer, retort).album) == 2
    assert payload(composer, retort).i18n_kwargs["content"] == ""


async def test_dispatcher_coalesces_album_before_throttling():
    dp = Dispatcher()
    dp.update.outer_middleware(MediaAlbumMiddleware(quiet_period=0.025))
    throttler = ThrottlingMiddleware()
    notifier = SimpleNamespace(notify_user=AsyncMock())
    container = SimpleNamespace(get=AsyncMock(return_value=notifier))
    handled = []

    async def throttle(handler, event, data):
        data[CONTAINER_KEY] = container
        data[USER_KEY] = SimpleNamespace(telegram_id=42, log="test")
        return await throttler.middleware_logic(handler, event, data)

    async def collect(message: Message, media_album=None):
        handled.append((message, media_album))

    dp.message.outer_middleware(throttle)
    dp.message.register(collect)
    bot = Bot("123456:TEST_TOKEN")
    try:
        await asyncio.gather(
            *[
                dp.feed_update(bot, update(photo(i, "Caption" if i == 1 else None)))
                for i in [3, 1, 2]
            ]
        )
        assert len(handled) == 1
        assert [m.message_id for m in handled[0][1]] == [1, 2, 3]
        assert handled[0][0].caption == "Caption"
        notifier.notify_user.assert_not_awaited()
        await dp.feed_update(bot, update(photo(10, group=None)))
        notifier.notify_user.assert_awaited_once()  # Normal spam protection remains enabled.
    finally:
        await bot.session.close()


async def test_duplicates_and_late_items_preserve_whole_group():
    middleware = MediaAlbumMiddleware(quiet_period=0.01)
    handler = AsyncMock()
    data = {"bot": SimpleNamespace(id=1)}
    await asyncio.gather(*[middleware(handler, update(photo(i)), data.copy()) for i in [1, 2, 2]])
    assert handler.await_count == 1
    await middleware(handler, update(photo(2)), data.copy())
    assert handler.await_count == 1
    await middleware(handler, update(photo(3)), data.copy())
    assert [m.message_id for m in handler.call_args.args[1]["media_album"]] == [1, 2, 3]
    assert middleware.pending == {}


async def test_item_arriving_during_handler_is_not_lost():
    middleware = MediaAlbumMiddleware(quiet_period=0.01)
    entered, release = asyncio.Event(), asyncio.Event()
    groups = []

    async def handler(event, data):
        groups.append([m.message_id for m in data["media_album"]])
        entered.set()
        await release.wait()

    data = {"bot": SimpleNamespace(id=1)}
    first = asyncio.create_task(middleware(handler, update(photo(1)), data.copy()))
    await entered.wait()
    late = asyncio.create_task(middleware(handler, update(photo(2)), data.copy()))
    release.set()
    await asyncio.gather(first, late)
    assert groups == [[1], [1, 2]]


async def test_preview_waits_for_album():
    middleware = MediaAlbumMiddleware(quiet_period=0.025)
    order = []

    async def handler(event, data):
        order.append("album" if event.message else "preview")

    data = {"bot": SimpleNamespace(id=1)}
    album = asyncio.create_task(middleware(handler, update(photo(1)), data.copy()))
    await asyncio.sleep(0)
    preview = Update(
        update_id=10,
        callback_query=CallbackQuery(
            id="preview",
            from_user=User(id=42, is_bot=False, first_name="Test"),
            chat_instance="instance",
            data="preview",
            message=photo(20, group=None),
        ),
    )
    await middleware(handler, preview, data.copy())
    await album
    assert order == ["album", "preview"]


async def test_failed_handler_is_retryable_and_releases_waiters():
    middleware = MediaAlbumMiddleware(quiet_period=0.001)
    data = {"bot": SimpleNamespace(id=1)}
    with pytest.raises(RuntimeError):
        await middleware(AsyncMock(side_effect=RuntimeError("failure")), update(photo()), data)
    assert middleware.pending == {} and not middleware.completed
    handler = AsyncMock()
    await middleware(handler, update(photo()), data)
    handler.assert_awaited_once()


@pytest.fixture
def service():
    svc = NotificationService(
        bot=SimpleNamespace(
            send_media_group=AsyncMock(return_value=[photo(i) for i in [1, 2, 3]]),
            send_message=AsyncMock(return_value=photo(4, group=None)),
            delete_messages=AsyncMock(return_value=True),
            delete_message=AsyncMock(),
        ),
        config=Mock(),
        translator_hub=Mock(),
        user_dao=Mock(),
        settings_dao=Mock(),
        worker=Mock(),
        event_publisher=Mock(),
        redis=SimpleNamespace(set=AsyncMock(), get=AsyncMock(), delete=AsyncMock()),
    )
    svc._get_translated_text = Mock(
        side_effect=lambda locale, key, args=None: (
            args["content"] if args else "Действия с публикацией ↑"
        )
    )
    svc._prepare_reply_markup = Mock(return_value=None)
    return svc


@pytest.fixture
def album_payload():
    return MessagePayloadDto(
        i18n_key="raw-message",
        i18n_kwargs={"content": '<b>Роутер</b> <tg-emoji emoji-id="123">⚡</tg-emoji>'},
        album=[
            AlbumMediaDto(
                media=MediaDescriptorDto(kind="file_id", value=f"photo-{i}"),
                media_type=MediaType.PHOTO,
            )
            for i in [1, 2, 3]
        ],
        delete_after=None,
    )


def recipient():
    return TempUserDto.as_temp_owner(telegram_id=42)


async def test_send_album_preserves_html_and_all_ids(service, album_payload):
    messages = await service.notify_broadcast(recipient(), album_payload)
    media = service.bot.send_media_group.call_args.kwargs["media"]
    assert [m.media for m in media] == ["photo-1", "photo-2", "photo-3"]
    assert media[0].caption == album_payload.i18n_kwargs["content"]
    assert media[0].parse_mode == "HTML" and media[1].caption is None
    assert [m.message_id for m in messages] == [1, 2, 3]
    service.bot.send_message.assert_not_awaited()


async def test_album_keyboard_and_close_delete_all_messages(service, album_payload):
    service._prepare_reply_markup.return_value = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Заказать", url="https://t.me/stealthnet_help")]
        ]
    )
    sent = await service.notify_broadcast(recipient(), album_payload)
    assert [m.message_id for m in sent] == [1, 2, 3, 4]
    assert service.bot.send_message.call_args.kwargs["reply_to_message_id"] == 1
    service.redis.get.return_value = service.redis.set.call_args.args[1]
    await service.delete_notification(42, 4)
    service.bot.delete_messages.assert_awaited_once_with(chat_id=42, message_ids=[1, 2, 3, 4])


async def test_partial_delivery_never_retries_already_sent_album(service, album_payload):
    service._prepare_reply_markup.return_value = Mock()
    service.bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=42, text="actions"), message="Flood wait", retry_after=1
    )
    with pytest.raises(PartialBroadcastDeliveryError) as error:
        await service.notify_broadcast(recipient(), album_payload)
    assert [m.message_id for m in error.value.messages] == [1, 2, 3]
    service.bot.send_media_group.assert_awaited_once()


async def test_single_notification_keeps_existing_path(service):
    service.notify_user = AsyncMock(return_value=photo(1))
    single = MessagePayloadDto(i18n_key="raw-message")
    assert len(await service.notify_broadcast(recipient(), single)) == 1
    service.notify_user.assert_awaited_once_with(recipient(), single)
    service.bot.send_media_group.assert_not_awaited()


def test_payload_round_trip_and_old_records(retort, album_payload):
    assert retort.load(retort.dump(album_payload), MessagePayloadDto) == album_payload
    assert retort.load({"i18n_key": "raw-message"}, MessagePayloadDto).album == []
    legacy = retort.load({"user_id": 1, "message_id": 100, "status": "SENT"}, BroadcastMessageDto)
    assert legacy.message_ids == [] and legacy.message_id == 100


def unwrap_task(task):
    return task.original_func.__dishka_orig_func__


@pytest.mark.parametrize("partial", [False, True])
async def test_worker_records_every_id_and_does_not_resend(partial, album_payload):
    user = SimpleNamespace(id=5, telegram_id=42, log="test")
    broadcast = BroadcastDto(
        id=1,
        task_id=uuid4(),
        status=BroadcastStatus.PROCESSING,
        audience=BroadcastAudience.ALL,
        payload=album_payload,
    )
    notifier = SimpleNamespace(
        notify_broadcast=AsyncMock(return_value=[photo(i) for i in [1, 2, 3]])
    )
    if partial:
        notifier.notify_broadcast.side_effect = PartialBroadcastDeliveryError([photo(1), photo(2)])
    update_status = SimpleNamespace(system=AsyncMock())
    finish = SimpleNamespace(system=AsyncMock())
    await unwrap_task(worker.send_broadcast_task)(
        broadcast,
        None,
        SimpleNamespace(get_by_task_id=AsyncMock(return_value=broadcast)),
        SimpleNamespace(system=AsyncMock(return_value=[user])),
        SimpleNamespace(
            system=AsyncMock(
                return_value=[
                    BroadcastMessageDto(
                        id=10, user_id=5, user_telegram_id=42, status=BroadcastMessageStatus.PENDING
                    )
                ]
            )
        ),
        update_status,
        finish,
        notifier,
    )
    notifier.notify_broadcast.assert_awaited_once()
    saved = update_status.system.call_args.args[0].messages[0]
    assert saved.message_ids == ([1, 2] if partial else [1, 2, 3])
    assert saved.message_id == 1
    assert saved.status == (
        BroadcastMessageStatus.FAILED if partial else BroadcastMessageStatus.SENT
    )


async def test_worker_deletes_album_partial_and_legacy_messages(monkeypatch, album_payload):
    monkeypatch.setattr(worker, "BATCH_DELAY", 0)
    broadcast = BroadcastDto(
        id=1,
        task_id=uuid4(),
        status=BroadcastStatus.COMPLETED,
        audience=BroadcastAudience.ALL,
        payload=album_payload,
        messages=[
            BroadcastMessageDto(
                user_id=1,
                user_telegram_id=41,
                message_id=1,
                message_ids=[1, 2, 3],
                status=BroadcastMessageStatus.SENT,
            ),
            BroadcastMessageDto(
                user_id=2, user_telegram_id=42, message_id=4, status=BroadcastMessageStatus.SENT
            ),
            BroadcastMessageDto(
                user_id=3,
                user_telegram_id=43,
                message_ids=[5, 6],
                status=BroadcastMessageStatus.FAILED,
            ),
        ],
    )
    bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
    result = await unwrap_task(worker.delete_broadcast_task)(
        broadcast,
        bot,
        SimpleNamespace(system=AsyncMock()),
        SimpleNamespace(system=AsyncMock()),
        SimpleNamespace(notify_admins=AsyncMock()),
    )
    assert result == (3, 3, 0)
    assert [c.kwargs["message_ids"] for c in bot.delete_messages.call_args_list] == [
        [1, 2, 3],
        [4],
        [5, 6],
    ]


def test_migration_sql_keeps_legacy_ids():

    migration = import_module(
        "src.infrastructure.database.migrations.versions.0042_add_broadcast_message_ids"
    )
    sql = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql}
    )
    with Operations.context(context):
        migration.upgrade()
    statement = sql.getvalue()
    assert "ADD COLUMN message_ids JSONB" in statement
    assert "jsonb_build_array(message_id)" in statement
    assert "DROP" not in statement


def test_dependency_graph_resolves_new_notification_dependency():
    config = SimpleNamespace()
    factory = Mock(spec=BgManagerFactory)
    assert create_aiogram_container(config, factory)
    assert create_taskiq_container(config, factory)
