"""Gift checkout, subscription renewal, and durable delivery for both participants."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from html import escape
from typing import Optional, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from adaptix import Retort
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from remnapy.models import UserResponseDto
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.application.common import Remnawave
from src.application.common.dao import (
    PlanDao,
    SettingsDao,
    SubscriptionDao,
    TransactionDao,
    UserDao,
)
from src.application.dto import PlanDto, PlanSnapshotDto, PriceDetailsDto, SubscriptionDto, UserDto
from src.application.services import PricingService
from src.core.enums import (
    AccessMode,
    Currency,
    PlanAvailability,
    SubscriptionStatus,
    TransactionStatus,
)
from src.core.utils.converters import days_to_datetime
from src.core.utils.time import datetime_now
from src.infrastructure.database.models import Gift, Subscription, Transaction, User


class GiftError(ValueError):
    """A message safe to show to the person purchasing/opening a gift."""


@dataclass(kw_only=True)
class GiftRecipient(UserDto):
    gift_uuid: UUID

    @property
    def remna_name(self) -> str:
        return f"gft_{self.gift_uuid.hex}"


def gift_period(days: int) -> str:
    return f"{days} дн." if days else "Без срока окончания"


def gift_status(gift: Gift, transaction: Optional[Transaction]) -> str:
    if not transaction:
        return "Не оплачен"
    if transaction.status == TransactionStatus.REFUNDED:
        return "Оплата возвращена"
    if gift.issued_at:
        if gift.expire_at and gift.expire_at <= datetime_now():
            return "Срок подписки закончился"
        return "Получатель открыл подарок" if gift.recipient_seen_at else "Подписка готова"
    if gift.paid_at:
        return "Оплачен · готовим подписку"
    if transaction.status == TransactionStatus.CANCELED:
        return "Оплата отменена"
    return "Ожидает оплаты"


class GiftService:
    def __init__(
        self,
        session: AsyncSession,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        transaction_dao: TransactionDao,
        plan_dao: PlanDao,
        settings_dao: SettingsDao,
        remnawave: Remnawave,
        retort: Retort,
        bot: Bot,
        pricing: PricingService,
    ) -> None:
        self.session = session
        self.users = user_dao
        self.subscriptions = subscription_dao
        self.transactions = transaction_dao
        self.plans = plan_dao
        self.settings = settings_dao
        self.remnawave = remnawave
        self.retort = retort
        self.bot = bot
        self.pricing = pricing

    async def create(
        self, buyer: UserDto, telegram_id: int, name: str, username: Optional[str]
    ) -> Gift:
        if not buyer.telegram_id or buyer.is_blocked:
            raise GiftError("Подарить VPN можно из личного чата с ботом.")
        if telegram_id <= 0 or telegram_id > 2**52 - 1 or telegram_id == buyer.telegram_id:
            raise GiftError("Выберите другого человека. Для себя используйте «Подписка».")
        await self._check_recipient(telegram_id)
        gift = Gift(
            token=uuid4(),
            buyer_id=buyer.id,
            recipient_telegram_id=telegram_id,
            recipient_name=name[:256],
            recipient_username=username,
            remna_uuid=uuid4(),
        )
        self.session.add(gift)
        await self.session.commit()
        return gift

    async def _check_recipient(self, telegram_id: int) -> None:
        settings = await self.settings.get()
        recipient = await self.users.get_by_telegram_id(telegram_id)
        if telegram_id in settings.blacklist.blocked_ids or (recipient and recipient.is_blocked):
            raise GiftError("Этому пользователю сейчас нельзя подарить подписку.")

    async def get(
        self, actor: UserDto, gift_id: int, *, buyer_only: bool = False, lock: bool = False
    ) -> Gift:
        condition = Gift.buyer_id == actor.id
        if not buyer_only and actor.telegram_id:
            condition = or_(condition, Gift.recipient_telegram_id == actor.telegram_id)
        query = select(Gift).where(Gift.id == gift_id, condition)
        if lock:
            query = query.with_for_update()
        gift = await self.session.scalar(query.execution_options(populate_existing=True))
        if not gift:
            raise GiftError("Подарок не найден или предназначен другому человеку.")
        return gift

    async def transaction(self, gift_id: int) -> Optional[Transaction]:
        result = await self.session.scalar(
            select(Transaction)
            .where(Transaction.gift_id == gift_id)
            .execution_options(populate_existing=True)
        )
        return cast(Optional[Transaction], result)

    async def available_plans(self) -> list[PlanDto]:
        return [
            p
            for p in await self.plans.get_active_plans()
            if p.availability == PlanAvailability.ALL and not p.is_trial
        ]

    async def quote(
        self, actor: UserDto, gift_id: int, plan_id: int, days: int, currency: Currency
    ) -> tuple[PlanSnapshotDto, PriceDetailsDto]:
        settings = await self.settings.get()
        if (
            actor.is_blocked
            or not settings.access.payments_allowed
            or settings.access.mode == AccessMode.RESTRICTED
        ):
            raise GiftError("Оплата подарков сейчас недоступна. Попробуйте позже.")
        gift = await self.get(actor, gift_id, buyer_only=True)
        await self._check_recipient(gift.recipient_telegram_id)
        plan = next((p for p in await self.available_plans() if p.id == plan_id), None)
        duration = plan.get_duration(days) if plan else None
        if not plan or not duration:
            raise GiftError("Тариф изменился. Выберите тариф заново.")
        # Gifts use the public tariff; the recipient's and buyer's one-time discounts stay intact.
        pricing = self.pricing.calculate_for_duration(
            actor,
            duration,
            currency,
            apply_discount=False,
            base_device_limit=plan.device_limit,
        )
        if pricing.final_amount <= 0:
            raise GiftError("Для подарка выберите платный тариф.")
        return PlanSnapshotDto.from_plan(plan, days), pricing

    async def validate_checkout(
        self,
        actor: UserDto,
        gift_id: int,
        snapshot: PlanSnapshotDto,
        pricing: PriceDetailsDto,
        currency: Currency,
    ) -> Optional[Transaction]:
        # The row lock also serializes repeated clicks while the provider creates the invoice.
        await self.get(actor, gift_id, buyer_only=True, lock=True)
        existing = await self.transaction(gift_id)
        if existing:
            return existing
        fresh_snapshot, fresh_pricing = await self.quote(
            actor,
            gift_id,
            snapshot.id,
            snapshot.duration,
            currency,
        )
        if snapshot != fresh_snapshot or pricing != fresh_pricing:
            raise GiftError("Условия тарифа изменились. Откройте подарок и выберите тариф заново.")
        return None

    async def _recipient(self, gift: Gift) -> UserDto:
        recipient = await self.users.get_by_telegram_id(gift.recipient_telegram_id)
        if recipient:
            return recipient
        buyer = await self.users.get_by_id(gift.buyer_id)
        candidate = UserDto(
            telegram_id=gift.recipient_telegram_id,
            name=gift.recipient_name,
            username=gift.recipient_username,
            referral_code=uuid4().hex,
            language=buyer.language if buyer else UserDto(name="").language,
        )
        values = self.retort.dump(candidate)
        for key in ("id", "created_at", "updated_at"):
            values.pop(key, None)
        # The receiver can press Start concurrently; Telegram identity remains unique.
        await self.session.execute(
            insert(User)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[User.telegram_id],
            )
        )
        recipient = await self.users.get_by_telegram_id(gift.recipient_telegram_id)
        if not recipient:
            raise RuntimeError("Gift recipient could not be created")
        return recipient

    @asynccontextmanager
    async def _recipient_lock(self, gift_id: int) -> AsyncIterator[None]:
        gift = await self.session.get(Gift, gift_id)
        if not gift:
            raise GiftError("Подарок не найден.")
        key = f"remnashop:gift:{gift.recipient_telegram_id}"
        await self.session.commit()
        # A separate transaction keeps this lock across durable provisioning checkpoints.
        # PostgreSQL releases it on rollback, cancellation, or a lost connection.
        engine = cast(AsyncEngine, self.session.bind)
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
            )
            yield

    async def complete(self, payment_id: UUID) -> None:
        transaction = await self.transactions.get_by_payment_id(payment_id)
        if not transaction or not transaction.gift_id:
            raise GiftError("Платёж подарка не найден.")
        async with self._recipient_lock(transaction.gift_id):
            await self._complete(payment_id)

    async def _complete(self, payment_id: UUID) -> None:
        transaction = await self.transactions.get_by_payment_id(payment_id)
        if not transaction or not transaction.gift_id:
            raise GiftError("Платёж подарка не найден.")
        gift = await self.session.scalar(
            select(Gift)
            .where(
                Gift.id == transaction.gift_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not gift:
            raise GiftError("Подарок не найден.")
        payment = await self.transaction(gift.id)
        if not payment or payment.status == TransactionStatus.REFUNDED:
            await self.session.commit()
            return
        if not gift.paid_at:
            gift.paid_at = datetime_now()
            gift.expire_at = (
                gift.paid_at + timedelta(days=transaction.plan_snapshot.duration)
                if transaction.plan_snapshot.duration
                else days_to_datetime(0)
            )
        payment.status = TransactionStatus.COMPLETED
        # Persist the creation identity and absolute expiry before the remote call.
        await self.session.commit()
        gift_id = gift.id
        await self._provision(gift_id, transaction.plan_snapshot)
        await self.notify(gift_id)

    async def _provision(self, gift_id: int, plan: PlanSnapshotDto) -> None:
        try:
            gift = await self.session.scalar(
                select(Gift)
                .where(Gift.id == gift_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not gift or not gift.paid_at or not gift.expire_at:
                raise RuntimeError("Gift has no confirmed payment")
            payment = await self.transaction(gift.id)
            if not payment or payment.status != TransactionStatus.COMPLETED or gift.issued_at:
                await self.session.commit()
                return
            recipient = await self._recipient(gift)
            if gift.renewal_base_expire_at:
                await self._renew(gift)
                return
            if await self._prepare_renewal(gift, recipient, plan):
                await self._provision(gift_id, plan)
                return
            subscription = SubscriptionDto(
                user_id=recipient.id,
                user_remna_id=gift.remna_uuid,
                traffic_limit=plan.traffic_limit,
                device_limit=plan.device_limit,
                traffic_limit_strategy=plan.traffic_limit_strategy,
                tag=plan.tag,
                internal_squads=plan.internal_squads,
                external_squad=plan.external_squad,
                expire_at=gift.expire_at,
                url="",
                plan_snapshot=plan,
            )
            identity = GiftRecipient(
                id=recipient.id,
                telegram_id=recipient.telegram_id,
                name=recipient.name,
                gift_uuid=gift.remna_uuid,
            )
            # v3 assigns numeric IDs. The persisted draft UUID supplies a unique username
            # for recovery if the panel creates a user but the local transaction rolls back.
            remote = await self._create_or_recover(identity, subscription)
            # Commit the actual panel identity atomically with issued_at and the subscription.
            gift.remna_uuid = remote.uuid
            subscription = replace(
                subscription,
                user_remna_id=remote.uuid,
                url=remote.subscription_url,
                status=SubscriptionStatus(remote.status),
            )
            values = self.retort.dump(subscription)
            for key in ("id", "created_at", "updated_at"):
                values.pop(key, None)
            values["user_remna_id"] = gift.remna_uuid
            local = Subscription(**values)
            self.session.add(local)
            await self.session.flush()
            # Serialize against another gift for this recipient. Keep existing paid access intact.
            user = await self.session.scalar(
                select(User).where(User.id == recipient.id).with_for_update()
            )
            current = await self.subscriptions.get_current(recipient.id)
            if user and (not current or current.is_expired or current.is_trial):
                user.current_subscription_id = local.id
            await self.users.set_trial_available(recipient.id, False)
            gift.subscription_id = local.id
            gift.issued_at = datetime_now()
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def _create_or_recover(
        self, identity: GiftRecipient, subscription: SubscriptionDto
    ) -> UserResponseDto:
        remote = await self.remnawave.get_user_by_username(identity.remna_name)
        if remote is None:
            try:
                remote = await self.remnawave.create_user(identity, subscription=subscription)
            except Exception:
                # A timeout can happen after the panel committed the creation.
                remote = await self.remnawave.get_user_by_username(identity.remna_name)
                if remote is None:
                    raise
        if remote.username != identity.remna_name or remote.telegram_id != identity.telegram_id:
            raise RuntimeError("Gift remote identity mismatch")
        return remote

    async def _prepare_renewal(self, gift: Gift, recipient: UserDto, plan: PlanSnapshotDto) -> bool:
        current = await self.subscriptions.get_current(recipient.id)
        if not current or current.is_trial:
            return False
        remote = await self.remnawave.get_user_by_uuid(current.user_remna_id)
        if not remote:
            return False
        if remote.telegram_id != gift.recipient_telegram_id or not gift.paid_at:
            raise RuntimeError("Gift remote identity mismatch")
        gift.renewal_base_expire_at = max(remote.expire_at, gift.paid_at)
        gift.expire_at = (
            gift.renewal_base_expire_at + timedelta(days=plan.duration)
            if plan.duration
            else max(gift.renewal_base_expire_at, days_to_datetime(0))
        )
        gift.remna_uuid = remote.uuid
        gift.subscription_id = current.id
        # A retry uses the same absolute expiry even if PATCH already succeeded.
        await self.session.commit()
        return True

    async def _renew(self, gift: Gift) -> None:
        local = await self.session.get(Subscription, gift.subscription_id)
        remote = await self.remnawave.get_user_by_uuid(gift.remna_uuid)
        if (
            not local
            or not remote
            or local.user_remna_id != gift.remna_uuid
            or remote.telegram_id != gift.recipient_telegram_id
            or not gift.expire_at
        ):
            raise RuntimeError("Gift renewal target is missing or mismatched")
        if remote.expire_at < gift.expire_at:
            remote = await self.remnawave.set_user_expiry(gift.remna_uuid, gift.expire_at)
        local.expire_at = remote.expire_at
        local.url = remote.subscription_url
        local.status = SubscriptionStatus(remote.status)
        gift.issued_at = datetime_now()
        await self.session.commit()

    async def refund(self, payment_id: UUID) -> None:
        transaction = await self.transactions.get_by_payment_id(payment_id)
        if not transaction or not transaction.gift_id:
            return
        async with self._recipient_lock(transaction.gift_id):
            await self._refund(payment_id)

    async def _refund(self, payment_id: UUID) -> None:
        transaction = await self.transactions.get_by_payment_id(payment_id)
        if not transaction or not transaction.gift_id:
            return
        try:
            gift = await self.session.scalar(
                select(Gift).where(Gift.id == transaction.gift_id).with_for_update()
            )
            payment = await self.transaction(transaction.gift_id)
            if not gift or not payment or not gift.paid_at:
                await self.session.commit()
                return
            if payment.status == TransactionStatus.REFUNDED:
                await self.session.commit()
                return
            remote = (
                await self.remnawave.get_user_by_uuid(gift.remna_uuid)
                if gift.issued_at or gift.renewal_base_expire_at
                else await self.remnawave.get_user_by_username(f"gft_{gift.remna_uuid.hex}")
            )
            if remote:
                if remote.telegram_id != gift.recipient_telegram_id:
                    raise RuntimeError("Gift remote identity mismatch")
                remote = await self._refund_remote(gift, remote)
            if gift.subscription_id:
                local = await self.session.get(Subscription, gift.subscription_id)
                if local:
                    if gift.refund_expire_at and remote:
                        local.expire_at = remote.expire_at
                        local.status = SubscriptionStatus(remote.status)
                    else:
                        local.status = SubscriptionStatus.DISABLED
            payment.status = TransactionStatus.REFUNDED
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def _refund_remote(self, gift: Gift, remote: UserResponseDto) -> UserResponseDto:
        another_gift = await self.session.scalar(
            select(Gift.id)
            .join(Transaction, Transaction.gift_id == Gift.id)
            .where(
                Gift.id != gift.id,
                Gift.remna_uuid == remote.uuid,
                Gift.issued_at.is_not(None),
                Transaction.status == TransactionStatus.COMPLETED,
            )
            .limit(1)
        )
        if gift.renewal_base_expire_at or another_gift:
            if not gift.refund_expire_at:
                base = gift.renewal_base_expire_at or gift.paid_at
                if not base:
                    raise RuntimeError("Gift refund has no paid period")
                credited = gift.expire_at - base if gift.expire_at else timedelta(0)
                applied = gift.issued_at or (gift.expire_at and remote.expire_at >= gift.expire_at)
                gift.refund_expire_at = remote.expire_at - credited if applied else remote.expire_at
                await self.session.commit()
            if remote.expire_at > gift.refund_expire_at:
                remote = await self.remnawave.set_user_expiry(remote.uuid, gift.refund_expire_at)
        else:
            await self.remnawave.disable_user(remote.uuid)
        return remote

    async def list_for(
        self, actor: UserDto, received: bool = False, offset: int = 0
    ) -> list[tuple[Gift, Optional[Transaction]]]:
        condition = (
            Gift.recipient_telegram_id == actor.telegram_id
            if received
            else Gift.buyer_id == actor.id
        )
        query = select(Gift, Transaction).outerjoin(Transaction, Transaction.gift_id == Gift.id)
        if received:
            query = query.where(Gift.paid_at.is_not(None))
        rows = await self.session.execute(
            query.where(condition).order_by(Gift.id.desc()).offset(offset).limit(10)
        )
        return [(row[0], row[1]) for row in rows]

    async def card(self, actor: UserDto, gift_id: int) -> tuple[str, InlineKeyboardMarkup]:  # noqa: C901
        gift = await self.get(actor, gift_id)
        payment = await self.transaction(gift.id)
        is_buyer = gift.buyer_id == actor.id
        if not is_buyer and not gift.paid_at:
            raise GiftError("Этот подарок ещё не оплачен.")
        if not is_buyer and gift.issued_at and not gift.recipient_seen_at:
            gift.recipient_seen_at = datetime_now()
            await self.session.commit()
        text = [
            "<b>Подарок VPN</b>",
            f"Кому: {escape(gift.recipient_name)}",
            f"Статус: {gift_status(gift, payment)}",
        ]
        if not is_buyer:
            buyer = await self.users.get_by_id(gift.buyer_id)
            if buyer:
                text.append(f"От кого: {escape(buyer.name)}")
        if gift.recipient_username:
            text.append(f"Telegram: @{escape(gift.recipient_username)}")
        rows: list[list[InlineKeyboardButton]] = []
        if payment:
            plan = self.retort.load(payment.plan_snapshot, PlanSnapshotDto)
            text.extend(
                [
                    f"Тариф: {escape(plan.name)} · {gift_period(plan.duration)}",
                    (
                        "Продление: параметры и ссылка текущей подписки сохранены"
                        if gift.renewal_base_expire_at
                        else f"Устройства: {plan.device_limit or 'Без лимита'}"
                    ),
                ]
            )
            if is_buyer:
                text.append(f"Оплата: {payment.pricing['final_amount']} {payment.currency.symbol}")
            if gift.expire_at:
                text.append(f"Действует до: {gift.expire_at:%d.%m.%Y %H:%M} UTC")
            if (
                payment.status == TransactionStatus.PENDING
                and payment.confirmation_url
                and is_buyer
            ):
                rows.append(
                    [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation_url)]
                )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🗓 Выбрать количество дней", callback_data=f"gift:plans:{gift.id}"
                    )
                ]
            )
        if gift.issued_at and payment and payment.status == TransactionStatus.COMPLETED:
            local = (
                await self.subscriptions.get_by_id(gift.subscription_id)
                if gift.subscription_id
                else None
            )
            if local and local.user_remna_id == gift.remna_uuid:
                text.append(f"\nСсылка подключения:\n<code>{escape(local.url)}</code>")
                rows.append([InlineKeyboardButton(text="🔗 Открыть подписку", url=local.url)])
            if is_buyer:
                me = await self.bot.get_me()
                invitation = f"https://t.me/{me.username}?start=gift_{gift.token.hex}"
                share = "https://t.me/share/url?" + urlencode(
                    {
                        "url": invitation,
                        "text": (
                            "Я подарил тебе VPN! Открой бота и нажми «Старт» — "
                            "подписка уже оплачена."
                        ),
                    }
                )
                rows.append([InlineKeyboardButton(text="📩 Отправить подарок", url=share)])
                text.append(
                    "\nПопросите получателя открыть бота и нажать «Старт». "
                    "Подарок и ссылка сохранятся в «Мои подарки»."
                )
            else:
                text.append(
                    "\nПодписка уже оплачена. Откройте ссылку и следуйте инструкции. "
                    "Ссылка также доступна тому, кто оплатил подарок."
                )
        rows.extend(
            [
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"gift:open:{gift.id}")],
                [
                    InlineKeyboardButton(
                        text="🎁 Мои подарки",
                        callback_data=f"gift:list:{'sent' if is_buyer else 'received'}:0",
                    )
                ],
                [InlineKeyboardButton(text="↩️ Главное меню", callback_data="gift:menu")],
            ]
        )
        return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=rows)

    def _recipient_notice(self, gift: Gift, payment: Transaction, buyer: Optional[UserDto]) -> str:
        sender = escape(buyer.name) if buyer else "Пользователь"
        days = payment.plan_snapshot["duration"]
        text = f"🎁 {sender} подарил вам VPN на {gift_period(days)}!"
        if gift.renewal_base_expire_at:
            text += "\nВаша текущая подписка продлена. Параметры и ссылка сохранены."
        else:
            text += "\nПодписка уже оплачена."
        if gift.expire_at:
            text += f"\nДействует до: {gift.expire_at:%d.%m.%Y %H:%M} UTC"
        return text

    async def notify(self, gift_id: int) -> None:
        gift = await self.session.scalar(
            select(Gift)
            .where(Gift.id == gift_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not gift or not gift.issued_at:
            return
        payment = await self.transaction(gift.id)
        if not payment or payment.status != TransactionStatus.COMPLETED:
            return
        buyer = await self.users.get_by_id(gift.buyer_id)
        recipient = await self.users.get_by_telegram_id(gift.recipient_telegram_id)
        for person, field in ((buyer, "buyer_notified_at"), (recipient, "recipient_notified_at")):
            if not person or not person.telegram_id or getattr(gift, field):
                continue
            unreachable_field = field.replace("notified", "unreachable")
            if getattr(gift, unreachable_field):
                continue
            try:
                if person.id == gift.buyer_id:
                    body, markup = await self.card(person, gift_id)
                    text = "🎁 Подарок оплачен!\n\n" + body
                else:
                    text = self._recipient_notice(gift, payment, buyer)
                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🎁 Открыть подарок", callback_data=f"gift:open:{gift.id}"
                                ),
                            ]
                        ]
                    )
                await self.bot.send_message(
                    person.telegram_id, text, reply_markup=markup, disable_web_page_preview=True
                )
                setattr(gift, field, datetime_now())
            except (TelegramForbiddenError, TelegramBadRequest):
                setattr(gift, unreachable_field, datetime_now())
                logger.info("Gift {} notification could not reach {}", gift.id, field)
            except Exception:
                logger.warning("Gift {} notification will be retried ({})", gift.id, field)
        await self.session.commit()

    async def recover(self) -> None:
        rows = await self.session.scalars(
            select(Transaction.payment_id)
            .join(
                Gift,
                Gift.id == Transaction.gift_id,
            )
            .where(
                Transaction.status == TransactionStatus.COMPLETED,
                Gift.paid_at.is_not(None),
                or_(
                    Gift.issued_at.is_(None),
                    (Gift.buyer_notified_at.is_(None) & Gift.buyer_unreachable_at.is_(None)),
                    (
                        Gift.recipient_notified_at.is_(None)
                        & Gift.recipient_unreachable_at.is_(None)
                    ),
                ),
            )
            .order_by(Gift.issued_at.asc().nulls_first(), Gift.id)
            .limit(50)
        )
        for payment_id in list(rows):
            try:
                await self.complete(payment_id)
            except Exception:
                await self.session.rollback()
                logger.error("Gift fulfillment will be retried for payment {}", payment_id)
