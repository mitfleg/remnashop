import asyncio
from typing import Optional, cast

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger

from src.application.common import Notifier
from src.application.common.dao import BroadcastDao
from src.application.common.notifier import PartialBroadcastDeliveryError
from src.application.dto import BroadcastDto, BroadcastMessageDto, MessagePayloadDto, UserDto
from src.application.use_cases.broadcast.commands.lifecycle import (
    FinishBroadcast,
    FinishBroadcastDto,
)
from src.application.use_cases.broadcast.commands.messages import (
    BulkUpdateBroadcastMessages,
    InitializeBroadcastMessages,
    InitializeBroadcastMessagesDto,
    UpdateBroadcastMessageStatus,
    UpdateBroadcastMessageStatusDto,
)
from src.application.use_cases.broadcast.queries.audience import (
    GetBroadcastAudienceUsers,
    GetBroadcastAudienceUsersDto,
)
from src.application.use_cases.misc.commands.maintenance import ClearOldBroadcasts
from src.core.constants import BATCH_DELAY, BATCH_SIZE_20
from src.core.enums import BroadcastMessageStatus, BroadcastStatus
from src.core.utils.iterables import chunked
from src.infrastructure.taskiq.broker import broker


@broker.task
@inject(patch_module=True)
async def send_broadcast_task(  # noqa: C901
    broadcast: BroadcastDto,
    plan_id: Optional[int],
    broadcast_dao: FromDishka[BroadcastDao],
    get_broadcast_audience_users: FromDishka[GetBroadcastAudienceUsers],
    initialize_broadcast_messages: FromDishka[InitializeBroadcastMessages],
    update_broadcast_message_status: FromDishka[UpdateBroadcastMessageStatus],
    finish_broadcast: FromDishka[FinishBroadcast],
    notifier: FromDishka[Notifier],
) -> None:
    task_id = broadcast.task_id

    try:
        users = await get_broadcast_audience_users.system(
            GetBroadcastAudienceUsersDto(broadcast.audience, plan_id)
        )

        # Broadcast is Telegram-only; exclude web-only users without a telegram_id
        users = [u for u in users if u.telegram_id is not None]

        if not users:
            logger.warning(f"No users found for broadcast '{task_id}'")
            await finish_broadcast.system(FinishBroadcastDto(task_id, BroadcastStatus.COMPLETED))
            return

        messages = []
        for user in users:
            messages.append(
                BroadcastMessageDto(
                    user_id=user.id,
                    user_telegram_id=user.telegram_id,
                    status=BroadcastMessageStatus.PENDING,
                )
            )

        messages = await initialize_broadcast_messages.system(
            InitializeBroadcastMessagesDto(task_id, messages)
        )

        total_users = len(users)
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        total_retry_time = 0.0
        semaphore = asyncio.Semaphore(BATCH_SIZE_20)

        logger.info(f"Started sending broadcast '{task_id}', total users: {total_users}")

        was_canceled = False

        async def send_one(user: UserDto) -> tuple:
            nonlocal total_retry_time
            status = BroadcastMessageStatus.FAILED
            msg_ids: list[int] = []
            retry_time_for_user = 0.0

            while True:
                try:
                    async with semaphore:
                        tg_messages = await notifier.notify_broadcast(user, broadcast.payload)

                    if tg_messages:
                        status = BroadcastMessageStatus.SENT
                        msg_ids = [message.message_id for message in tg_messages]

                    return user.id, user.telegram_id, status, msg_ids, retry_time_for_user

                except PartialBroadcastDeliveryError as error:
                    msg_ids = [message.message_id for message in error.messages]
                    logger.error(f"Partially delivered broadcast to {user.log}")
                    return user.id, user.telegram_id, status, msg_ids, retry_time_for_user
                except TelegramRetryAfter as error:
                    wait_time = error.retry_after + BATCH_DELAY
                    logger.warning(f"Flood wait {error.retry_after}s for user {user.log}")
                    await asyncio.sleep(wait_time)
                    retry_time_for_user += wait_time
                    total_retry_time += wait_time
                except Exception:
                    logger.exception(f"Failed to send to {user.log}")
                    return user.id, user.telegram_id, status, msg_ids, retry_time_for_user

        for i, batch in enumerate(chunked(users, BATCH_SIZE_20), start=1):
            # Check cancellation every batch (a single cheap SELECT) so a cancel request
            # stops sending promptly instead of after up to ~100 more messages.
            current = await broadcast_dao.get_by_task_id(task_id)
            if not current or current.status == BroadcastStatus.CANCELED:
                logger.info(f"Broadcast '{task_id}' was canceled")
                was_canceled = True
                break

            tasks = [asyncio.create_task(send_one(user)) for user in batch]
            results = await asyncio.gather(*tasks)

            updates = UpdateBroadcastMessageStatusDto(
                task_id=task_id,
                messages=[
                    BroadcastMessageDto(
                        id=next(m.id for m in messages if m.user_id == uid),
                        user_id=uid,
                        user_telegram_id=tg_id,
                        status=status,
                        message_id=msg_ids[0] if msg_ids else None,
                        message_ids=msg_ids,
                    )
                    for uid, tg_id, status, msg_ids, _ in results
                ],
            )

            await update_broadcast_message_status.system(updates)

            sent_count = sum(
                1 for _, _, status, _, _ in results if status == BroadcastMessageStatus.SENT
            )
            failed_count = len(results) - sent_count
            batch_retry_time = sum(r[4] for r in results)

            logger.info(
                f"Batch {i}: sent={sent_count}, failed={failed_count}, "
                f"retry_time={batch_retry_time:.2f}s"
            )

        total_elapsed = loop.time() - start_time
        final_status = BroadcastStatus.CANCELED if was_canceled else BroadcastStatus.COMPLETED
        await finish_broadcast.system(FinishBroadcastDto(task_id, final_status))
        logger.success(
            f"Broadcast '{task_id}' finished in {total_elapsed:.2f}s "
            f"with total retry time {total_retry_time:.2f}s"
        )

    except Exception:
        logger.exception(f"Broadcast '{task_id}' failed with an unexpected error")
        await finish_broadcast.system(FinishBroadcastDto(task_id, BroadcastStatus.ERROR))
        return


async def _finalize_broadcast_deletion(
    finish_broadcast: FinishBroadcast,
    notifier: Notifier,
    broadcast: BroadcastDto,
    total: int,
    deleted: int,
) -> None:
    await finish_broadcast.system(
        FinishBroadcastDto(task_id=broadcast.task_id, status=BroadcastStatus.DELETED)
    )
    await notifier.notify_admins(
        MessagePayloadDto(
            i18n_key="ntf-broadcast.deleted-success",
            i18n_kwargs={
                "task_id": str(broadcast.task_id),
                "total_count": total,
                "deleted_count": deleted,
                "failed_count": total - deleted,
            },
            disable_default_markup=False,
        )
    )


@broker.task
@inject(patch_module=True)
async def delete_broadcast_task(
    broadcast: BroadcastDto,
    bot: FromDishka[Bot],
    bulk_update_broadcast_messages: FromDishka[BulkUpdateBroadcastMessages],
    finish_broadcast: FromDishka[FinishBroadcast],
    notifier: FromDishka[Notifier],
) -> tuple[int, int, int]:
    broadcast_id = cast(int, broadcast.id)

    if not broadcast.messages:
        logger.warning(f"No messages to delete for broadcast '{broadcast_id}'")
        await _finalize_broadcast_deletion(finish_broadcast, notifier, broadcast, 0, 0)
        return 0, 0, 0

    logger.info(f"Started deleting messages for broadcast '{broadcast_id}'")

    deleted_count = 0
    total_messages = len(broadcast.messages)
    total_retry_time = 0.0

    loop = asyncio.get_running_loop()
    start_time = loop.time()
    semaphore = asyncio.Semaphore(BATCH_SIZE_20)

    async def delete_one(message: BroadcastMessageDto) -> tuple[BroadcastMessageDto, float]:
        nonlocal total_retry_time
        retry_time_for_msg = 0.0

        if (
            message.status == BroadcastMessageStatus.DELETED
            or not (message.message_ids or message.message_id)
            or not message.user_telegram_id
        ):
            return message, retry_time_for_msg

        user_telegram_id: int = message.user_telegram_id
        while True:
            try:
                async with semaphore:
                    ids = message.message_ids or [cast(int, message.message_id)]
                    if await bot.delete_messages(chat_id=user_telegram_id, message_ids=ids):
                        message.status = BroadcastMessageStatus.DELETED

                return message, retry_time_for_msg

            except TelegramRetryAfter as error:
                wait_time = error.retry_after + BATCH_DELAY
                logger.warning(
                    f"Flood wait {error.retry_after}s for user '{message.user_telegram_id}'"
                )
                await asyncio.sleep(wait_time)
                retry_time_for_msg += wait_time
                total_retry_time += wait_time

            except Exception:
                logger.exception(
                    f"Exception deleting message for user '{message.user_telegram_id}'"
                )
                return message, retry_time_for_msg

    for i, batch in enumerate(chunked(broadcast.messages, BATCH_SIZE_20), start=1):
        tasks = [asyncio.create_task(delete_one(m)) for m in batch]
        results = await asyncio.gather(*tasks)

        updated_messages = [r[0] for r in results]
        batch_retry_time = sum(r[1] for r in results)

        await bulk_update_broadcast_messages.system(updated_messages)

        batch_deleted = sum(
            1 for m in updated_messages if m.status == BroadcastMessageStatus.DELETED
        )
        deleted_count += batch_deleted

        logger.info(f"Batch {i}: deleted={batch_deleted}, retry_time={batch_retry_time:.2f}s")

        if batch_retry_time == 0:
            await asyncio.sleep(BATCH_DELAY)

    total_elapsed = loop.time() - start_time
    logger.success(
        f"Deletion finished for broadcast '{broadcast_id}' "
        f"Total: {total_messages}, Deleted: {deleted_count}, "
        f"Time: {total_elapsed:.2f}s, Retry time: {total_retry_time:.2f}s"
    )

    await _finalize_broadcast_deletion(
        finish_broadcast, notifier, broadcast, total_messages, deleted_count
    )
    return total_messages, deleted_count, total_messages - deleted_count


@broker.task(schedule=[{"cron": "0 0 */7 * *"}])
@inject(patch_module=True)
async def delete_broadcasts_task(clear_old_broadcasts: FromDishka[ClearOldBroadcasts]) -> None:
    await clear_old_broadcasts.system()
