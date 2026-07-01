from dataclasses import dataclass
from datetime import timedelta

from loguru import logger

from src.application.common import EventPublisher, Interactor, Remnawave
from src.application.common.dao import SettingsDao, SubscriptionDao
from src.application.common.uow import UnitOfWork
from src.application.dto import SubscriptionDto, UserDto
from src.application.events import GraceActivatedEvent, SubscriptionExpiredEvent
from src.core.utils.converters import days_to_datetime
from src.core.utils.time import datetime_now

_MB = 1024**2


@dataclass(frozen=True)
class EnterGraceModeDto:
    user: UserDto
    subscription: SubscriptionDto


class EnterGraceMode(Interactor[EnterGraceModeDto, None]):
    required_permission = None

    def __init__(
        self,
        uow: UnitOfWork,
        settings_dao: SettingsDao,
        subscription_dao: SubscriptionDao,
        remnawave: Remnawave,
        event_publisher: EventPublisher,
    ) -> None:
        self.uow = uow
        self.settings_dao = settings_dao
        self.subscription_dao = subscription_dao
        self.remnawave = remnawave
        self.event_publisher = event_publisher

    async def _execute(self, actor: UserDto, data: EnterGraceModeDto) -> None:
        user = data.user
        sub = data.subscription
        grace = (await self.settings_dao.get()).grace

        if not grace.enabled or sub.is_trial:
            await self.event_publisher.publish(
                SubscriptionExpiredEvent(user=user, is_trial=sub.is_trial)
            )
            return

        async with self.uow:
            if sub.grace_until is None:
                grace_until = (
                    days_to_datetime(0)
                    if grace.is_indefinite
                    else datetime_now() + timedelta(days=grace.duration_days)
                )
                await self.remnawave.apply_grace(
                    uuid=sub.user_remna_id,
                    expire_at=grace_until,
                    internal_squads=grace.internal_squads,
                    external_squad=grace.external_squad,
                    traffic_bytes=grace.traffic_mb * _MB,
                    traffic_strategy=grace.traffic_strategy,
                    tag=grace.tag,
                    device_limit=sub.device_limit,
                )
                sub.grace_until = grace_until
                await self.subscription_dao.update(sub)
                await self.uow.commit()
                logger.info(f"{actor.log} Activated grace for '{sub.user_remna_id}'")
                await self.event_publisher.publish(
                    GraceActivatedEvent(
                        user=user,
                        is_trial=sub.is_trial,
                        traffic_mb=grace.traffic_mb,
                    )
                )
            else:
                await self.remnawave.disable_user(sub.user_remna_id)
                sub.grace_until = None
                await self.subscription_dao.update(sub)
                await self.uow.commit()
                logger.info(f"{actor.log} Ended grace for '{sub.user_remna_id}'")
                await self.event_publisher.publish(
                    SubscriptionExpiredEvent(user=user, is_trial=sub.is_trial)
                )
