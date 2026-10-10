from typing import Any, Optional
from uuid import UUID

from adaptix import Retort
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.utils import remove_intent_id
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Select
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import BotService, Notifier, TranslatorRunner
from src.application.common.dao import BroadcastDao, SettingsDao
from src.application.dto import (
    AlbumMediaDto,
    MediaDescriptorDto,
    MessagePayloadDto,
    TelegramUserDto,
)
from src.application.use_cases.broadcast.commands.lifecycle import (
    CancelBroadcast,
    DeleteBroadcast,
    StartBroadcast,
    StartBroadcastDto,
)
from src.application.use_cases.broadcast.queries.audience import (
    GetBroadcastAudienceCount,
    GetBroadcastAudienceCountDto,
    HasAvailableBroadcastPlans,
)
from src.core.constants import TEXT_MAX_LENGTH, TEXT_MEDIA_MAX_LENGTH, USER_KEY
from src.core.enums import BroadcastAudience, MediaType
from src.telegram.keyboards import CLOSE_BUTTON_ID, get_broadcast_buttons
from src.telegram.states import DashboardBroadcast
from src.telegram.utils import is_double_click


def _update_payload(
    dialog_manager: DialogManager,
    retort: Retort,
    **updates: Any,
) -> MessagePayloadDto:
    raw_payload = dialog_manager.dialog_data.get("payload")

    old_payload = (
        retort.load(raw_payload, MessagePayloadDto)
        if raw_payload
        else MessagePayloadDto(
            i18n_key="raw-message",
            disable_default_markup=False,
            delete_after=None,
        )
    )
    payload_data: dict = retort.dump(old_payload, MessagePayloadDto)
    payload_data.update(updates)
    dialog_manager.dialog_data["payload"] = payload_data
    return retort.load(payload_data, MessagePayloadDto)


@inject
async def on_broadcast_list(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    broadcast_dao: FromDishka[BroadcastDao],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    broadcasts = await broadcast_dao.get_all()

    if not broadcasts:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.list-empty")
        return

    await dialog_manager.switch_to(state=DashboardBroadcast.LIST)


@inject
async def on_broadcast_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_broadcast: UUID,
) -> None:
    dialog_manager.dialog_data["task_id"] = selected_broadcast
    await dialog_manager.switch_to(state=DashboardBroadcast.VIEW)


@inject
async def on_audience_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    get_broadcast_audience_count: FromDishka[GetBroadcastAudienceCount],
    has_available_plans: FromDishka[HasAvailableBroadcastPlans],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]

    if not callback.data:
        raise ValueError("Callback data is empty")

    audience = BroadcastAudience(remove_intent_id(callback.data)[-1])
    dialog_manager.dialog_data["audience_type"] = audience

    if audience == BroadcastAudience.PLAN:
        # The audience size is per-plan; here we only gate on plan availability.
        if not await has_available_plans(user):
            await notifier.notify_user(user, i18n_key="ntf-broadcast.plans-unavailable")
            return
        await dialog_manager.switch_to(state=DashboardBroadcast.PLAN)
        return

    audience_count = await get_broadcast_audience_count(
        user, GetBroadcastAudienceCountDto(audience)
    )
    if audience_count == 0:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.audience-unavailable")
        return

    dialog_manager.dialog_data["audience_count"] = audience_count
    await dialog_manager.switch_to(state=DashboardBroadcast.SEND)


@inject
async def on_plan_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_plan_id: int,
    notifier: FromDishka[Notifier],
    get_broadcast_audience_count: FromDishka[GetBroadcastAudienceCount],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]

    audience_count = await get_broadcast_audience_count(
        user,
        GetBroadcastAudienceCountDto(audience=BroadcastAudience.PLAN, plan_id=selected_plan_id),
    )

    if audience_count == 0:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.audience-unavailable")
        return

    dialog_manager.dialog_data["plan_id"] = selected_plan_id
    dialog_manager.dialog_data["audience_count"] = audience_count
    await dialog_manager.switch_to(state=DashboardBroadcast.SEND)


def _extract_media(message: Message, *, single: bool) -> Optional[AlbumMediaDto]:
    if message.photo:
        kind, file_id = MediaType.PHOTO, message.photo[-1].file_id
    elif message.video:
        kind, file_id = MediaType.VIDEO, message.video.file_id
    elif message.animation and single:
        kind, file_id = MediaType.GIF, message.animation.file_id
    elif message.document:
        kind, file_id = MediaType.DOCUMENT, message.document.file_id
    elif message.sticker and single:
        kind, file_id = MediaType.DOCUMENT, message.sticker.file_id
    else:
        return None
    return AlbumMediaDto(
        media=MediaDescriptorDto(kind="file_id", value=file_id),
        media_type=kind,
    )


@inject
async def on_content_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]

    messages: list[Message] = dialog_manager.middleware_data.get("media_album", [message])
    media_items: list[AlbumMediaDto] = []
    captions = [item for item in messages if item.text or item.caption]
    caption_message = captions[0] if captions else message
    plain_text = caption_message.text or caption_message.caption or ""

    for item in messages:
        media = _extract_media(item, single=len(messages) == 1)
        if media is None:
            if len(messages) > 1:
                await notifier.notify_user(user, i18n_key="ntf-broadcast.album-invalid")
                return
            continue
        media_items.append(media)

    if (
        len(media_items) > 10
        or len(captions) > 1
        or (
            len(media_items) > 1
            and any(item.media_type == MediaType.DOCUMENT for item in media_items)
            and not all(item.media_type == MediaType.DOCUMENT for item in media_items)
        )
    ):
        await notifier.notify_user(user, i18n_key="ntf-broadcast.album-invalid")
        return
    if not (plain_text or media_items):
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return

    # Telegram limits the actual text, not serialized HTML/custom-emoji tags.
    max_length = TEXT_MEDIA_MAX_LENGTH if media_items else TEXT_MAX_LENGTH
    text_length = len(plain_text.encode("utf-16-le")) // 2
    if text_length > max_length:
        logger.warning(f"{user.log} Message text exceeds limit: '{text_length}' > '{max_length}'")
        await notifier.notify_user(
            user,
            MessagePayloadDto(
                i18n_key="ntf-broadcast.text-too-long",
                i18n_kwargs={"max_limit": max_length},
            ),
        )
        return

    # Keep escaped HTML intact; unescape would turn literal < and & into markup.
    single = media_items[0] if len(media_items) == 1 else None
    _update_payload(
        dialog_manager,
        retort,
        i18n_kwargs={"content": caption_message.html_text},
        media_type=single.media_type if single else None,
        media=retort.dump(single.media) if single else None,
        album=retort.dump(media_items, list[AlbumMediaDto]) if len(media_items) > 1 else [],
    )
    logger.info(f"{user.log} Updated message payload ({len(media_items)} media items)")
    await notifier.notify_user(user, i18n_key="ntf-broadcast.content-saved")


@inject
async def on_button_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    bot_service: FromDishka[BotService],
    retort: FromDishka[Retort],
    i18n: FromDishka[TranslatorRunner],
    settings_dao: FromDishka[SettingsDao],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    selected_id = int(dialog_manager.item_id)  # type: ignore[attr-defined]

    buttons: list[dict] = dialog_manager.dialog_data.get("buttons", [])
    for button in buttons:
        if button["id"] == selected_id:
            button["selected"] = not button.get("selected", False)
            break

    settings = await settings_dao.get()

    all_buttons = get_broadcast_buttons(
        support_url=bot_service.get_support_url(text=i18n.get("message.help")),
        is_referral_enable=settings.referral.enable,
    )
    goto_buttons = all_buttons[:-1]

    if selected_id == CLOSE_BUTTON_ID:
        close_selected = next((b["selected"] for b in buttons if b["id"] == CLOSE_BUTTON_ID), True)
        _update_payload(dialog_manager, retort, disable_default_markup=not close_selected)
    else:
        builder = InlineKeyboardBuilder()
        for button in buttons:
            if button.get("selected") and button["id"] != CLOSE_BUTTON_ID:
                builder.row(goto_buttons[int(button["id"])])
        _update_payload(dialog_manager, retort, reply_markup=builder.as_markup().model_dump())

    logger.debug(f"{user.log} Updated payload keyboard: {buttons}")


@inject
async def on_preview(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    payload = dialog_manager.dialog_data.get("payload")

    if (
        not payload
        or not payload["i18n_kwargs"].get("content")
        and not payload.get("media")
        and not payload.get("album")
    ):
        await notifier.notify_user(user, i18n_key="ntf-broadcast.content-empty")
        return

    await notifier.notify_broadcast(user, retort.load(payload, MessagePayloadDto))


@inject
async def on_view_preview(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    broadcast_dao: FromDishka[BroadcastDao],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    task_id = dialog_manager.dialog_data.get("task_id")
    broadcast = await broadcast_dao.get_by_task_id(task_id) if task_id else None

    if not broadcast or not broadcast.payload:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.content-empty")
        return

    await notifier.notify_broadcast(user, broadcast.payload)


@inject
async def on_send(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
    start_broadcast: FromDishka[StartBroadcast],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    audience: Optional[BroadcastAudience] = dialog_manager.dialog_data.get("audience_type")
    plan_id = dialog_manager.dialog_data.get("plan_id")
    payload = dialog_manager.dialog_data.get("payload")

    if not payload:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.content-empty")
        return

    payload = retort.load(payload, MessagePayloadDto)

    if not audience:
        raise ValueError("BroadcastAudience not found in dialog data")

    if is_double_click(dialog_manager, key="broadcast_confirm", cooldown=5):
        task_id = await start_broadcast(user, StartBroadcastDto(audience, payload, plan_id))
        dialog_manager.dialog_data["task_id"] = task_id
        await dialog_manager.switch_to(state=DashboardBroadcast.VIEW)
        return

    await notifier.notify_user(user, i18n_key="ntf-common.double-click-confirm")
    logger.debug(f"{user.log} Awaiting confirmation for broadcast send")


@inject
async def on_cancel(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    cancel_broadcast: FromDishka[CancelBroadcast],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    task_id = dialog_manager.dialog_data["task_id"]

    try:
        await cancel_broadcast(user, task_id)
        await notifier.notify_user(user, i18n_key="ntf-broadcast.canceled")
    except ValueError:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.not-cancelable")


@inject
async def on_delete(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    delete_broadcast: FromDishka[DeleteBroadcast],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    task_id = dialog_manager.dialog_data["task_id"]

    try:
        # Deletion runs in the background; respond immediately. The task reports the
        # result summary to admins on completion (see delete_broadcast_task).
        await delete_broadcast(user, task_id)
        await notifier.notify_user(user, i18n_key="ntf-broadcast.deleting")
    except ValueError:
        await notifier.notify_user(user, i18n_key="ntf-broadcast.already-deleted")
