import asyncio
import io
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    SharedUser,
    Update,
    UsersShared,
)
from aiogram.types import (
    User as TelegramUser,
)
from alembic.migration import MigrationContext
from alembic.operations import Operations
from dishka import Provider, Scope, from_context, make_async_container
from dishka.integrations.aiogram import setup_dishka
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.application.common import TranslatorRunner
from src.application.common.dao import PaymentGatewayDao
from src.application.dto import (
    PlanDto,
    PlanDurationDto,
    PlanPriceDto,
    PlanSnapshotDto,
    PriceDetailsDto,
    RemnaSubscriptionDto,
    SubscriptionDto,
    TelegramUserDto,
    TransactionDto,
    UserDto,
)
from src.application.services import PricingService
from src.core.config import AppConfig
from src.core.enums import (
    AccessMode,
    Currency,
    PaymentGatewayType,
    PlanAvailability,
    PlanType,
    PurchaseType,
    Role,
    TransactionStatus,
)
from src.core.utils.time import datetime_now

with patch.object(
    AppConfig,
    "get",
    return_value=SimpleNamespace(
        redis=SimpleNamespace(dsn="redis://localhost/15"),
    ),
):
    from src.application.use_cases.gateways.commands.payment import (
        CreatePayment,
        CreatePaymentDto,
        ProcessPayment,
        ProcessPaymentDto,
    )
    from src.application.use_cases.remnawave.commands.synchronization import (
        SyncRemnaUser,
        SyncRemnaUserDto,
    )
    from src.infrastructure.database.dao.subscription import SubscriptionDaoImpl
    from src.infrastructure.database.dao.transaction import TransactionDaoImpl
    from src.infrastructure.database.dao.user import UserDaoImpl
    from src.infrastructure.database.models import BaseSql, Subscription, User
    from src.infrastructure.database.uow import UnitOfWorkImpl
    from src.infrastructure.di.providers.retort import RetortProvider
    from src.infrastructure.services.gifts import GiftError, GiftRecipient, GiftService
    from src.infrastructure.services.remnawave import RemnawaveImpl
    from src.telegram.routers.extra.payment import on_pre_checkout, on_successful_payment
    from src.telegram.routers.gifts import (
        on_gift_callback,
        on_recipient,
        picker_key,
        start_picker,
    )


def plan():
    return PlanDto(
        id=5,
        name="Premium",
        type=PlanType.DEVICES,
        device_limit=5,
        traffic_limit=0,
        is_active=True,
        durations=[
            PlanDurationDto(
                days=30,
                prices=[PlanPriceDto(currency=Currency.RUB, price=Decimal(300))],
            )
        ],
    )


def settings():
    return SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                blacklist=SimpleNamespace(blocked_ids=[]),
                access=SimpleNamespace(mode=AccessMode.PUBLIC, payments_allowed=True),
            )
        )
    )


async def test_native_picker_allows_people_outside_bot_and_one_recipient():
    bot = SimpleNamespace(id=100, send_message=AsyncMock())
    redis = SimpleNamespace(set=AsyncMock())
    user = TelegramUserDto(id=1, telegram_id=123, name="Buyer")
    await start_picker(bot, user, redis)
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    request = markup.keyboard[0][0].request_users
    assert request.user_is_bot is False and request.max_quantity == 1
    assert request.request_name and request.request_username
    assert redis.set.call_args.args[0] == picker_key(bot.id, user.id)
    assert redis.set.call_args.kwargs["ex"] == 900


@pytest.mark.parametrize("request_id,users", [(99, [555]), (42, [555, 666])])
async def test_stale_or_multiple_recipient_selections_never_create_gift(request_id, users):
    message = Message(
        message_id=1,
        date=0,
        chat=Chat(id=123, type="private"),
        users_shared=UsersShared(
            request_id=request_id, users=[SharedUser(user_id=i) for i in users]
        ),
    )
    service = SimpleNamespace(create=AsyncMock())
    bot = SimpleNamespace(id=100)
    redis = SimpleNamespace(get=AsyncMock(return_value=b"42"))
    with patch.object(Message, "answer", new_callable=AsyncMock):
        await on_recipient.__dishka_orig_func__(
            message,
            TelegramUserDto(id=1, telegram_id=123, name="Buyer"),
            service,
            redis,
            bot,
        )
    service.create.assert_not_awaited()


async def test_payment_router_sends_gift_to_its_service_not_buyers_subscription():
    transaction = TransactionDto(
        payment_id=uuid4(),
        user_id=1,
        gift_id=2,
        status=TransactionStatus.PENDING,
        purchase_type=PurchaseType.NEW,
        gateway_type=PaymentGatewayType.YOOKASSA,
        currency=Currency.RUB,
        pricing=PriceDetailsDto.test(),
        plan_snapshot=PlanSnapshotDto.from_plan(plan(), 30),
    )
    uow = AsyncMock()
    service = SimpleNamespace(complete=AsyncMock(), refund=AsyncMock())
    purchase = SimpleNamespace(system=AsyncMock())
    process = ProcessPayment(
        uow,
        SimpleNamespace(get_by_id=AsyncMock(return_value=UserDto(id=1, name="Buyer"))),
        SimpleNamespace(get_by_payment_id=AsyncMock(return_value=transaction)),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        purchase,
        service,
    )
    await process.system(
        ProcessPaymentDto(
            transaction.payment_id, TransactionStatus.COMPLETED, PaymentGatewayType.YOOKASSA
        )
    )
    service.complete.assert_awaited_once_with(transaction.payment_id)
    purchase.system.assert_not_awaited()
    service.complete.reset_mock()
    await process.system(
        ProcessPaymentDto(
            transaction.payment_id, TransactionStatus.COMPLETED, PaymentGatewayType.PLATEGA
        )
    )
    service.complete.assert_not_awaited()


def test_migration_preserves_existing_payments_and_broadcast_revision():
    migration = import_module("src.infrastructure.database.migrations.versions.0043_add_gifts")
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
        },
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "recipient_telegram_id BIGINT NOT NULL" in sql
    assert "UNIQUE (gift_id)" in sql and "confirmation_url" in sql
    assert "DROP" not in sql
    assert migration.down_revision == "0042"


@pytest.fixture
async def store():
    dsn = os.environ.get("GIFT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("GIFT_TEST_DATABASE_URL is required for isolated PostgreSQL tests")
    if not dsn.startswith(
        "postgresql+asyncpg://gift_test:gift_test_local@127.0.0.1:55439/gift_test"
    ):
        pytest.fail("Gift database tests only accept the dedicated local test container")
    schema = f"gift_test_{uuid4().hex}"
    admin = create_async_engine(dsn, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        dsn, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(BaseSql.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    retort = RetortProvider().get_retort()
    converter = RetortProvider().get_conversion_retort(retort, Mock())
    panel = {}

    async def remote_create(user, *, subscription):
        remote = SimpleNamespace(
            uuid=subscription.user_remna_id,
            telegram_id=user.telegram_id,
            username=user.remna_name,
            status="ACTIVE",
            subscription_url=f"https://example.test/sub/{subscription.user_remna_id}",
            expire_at=subscription.expire_at,
        )
        panel[remote.uuid] = remote
        return remote

    remote = SimpleNamespace(
        get_user_by_uuid=AsyncMock(side_effect=lambda key: panel.get(key)),
        create_user=AsyncMock(side_effect=remote_create),
        disable_user=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_gift_bot")),
    )
    config = settings()
    catalog = SimpleNamespace(get_active_plans=AsyncMock(return_value=[plan()]))

    @asynccontextmanager
    async def service():
        async with sessions() as session:
            users = UserDaoImpl(session, retort, converter, Mock())
            subs = SubscriptionDaoImpl(session, retort, converter, Mock(), users)
            transactions = TransactionDaoImpl(session, retort, converter, Mock())
            yield GiftService(
                session,
                users,
                subs,
                transactions,
                catalog,
                config,
                remote,
                retort,
                bot,
                PricingService(),
            )

    async with service() as svc:
        buyer = await svc.users.create(
            UserDto(name="Buyer", telegram_id=111, referral_code=uuid4().hex)
        )
        await svc.session.commit()

    async def order(telegram_id=222):
        async with service() as svc:
            gift = await svc.create(buyer, telegram_id, "Recipient <name>", "recipient")
            payment = await svc.transactions.create(
                TransactionDto(
                    payment_id=uuid4(),
                    user_id=buyer.id,
                    gift_id=gift.id,
                    status=TransactionStatus.PENDING,
                    purchase_type=PurchaseType.NEW,
                    gateway_type=PaymentGatewayType.YOOKASSA,
                    currency=Currency.RUB,
                    confirmation_url="https://example.test/pay",
                    pricing=PriceDetailsDto(
                        original_amount=Decimal(300), final_amount=Decimal(300), discount_percent=0
                    ),
                    plan_snapshot=PlanSnapshotDto.from_plan(plan(), 30),
                )
            )
            await svc.session.commit()
            return gift.id, payment.payment_id

    yield SimpleNamespace(
        service=service,
        order=order,
        buyer=buyer,
        remote=remote,
        panel=panel,
        bot=bot,
        retort=retort,
        config=config,
        catalog=catalog,
        engine=engine,
    )
    await engine.dispose()
    async with admin.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin.dispose()


async def test_new_recipient_issued_before_first_start_and_both_can_reopen(store):
    gift_id, payment_id = await store.order()
    async with store.service() as svc:
        await svc.complete(payment_id)
    async with store.service() as svc:
        recipient = await svc.users.get_by_telegram_id(222)
        assert recipient and not recipient.is_rules_accepted
        assert recipient.purchase_discount == 0
        own = await svc.subscriptions.get_current(store.buyer.id)
        assert own is None
        sub = await svc.subscriptions.get_current(recipient.id)
        assert sub and sub.plan_snapshot.duration == 30
        gift = await svc.get(store.buyer, gift_id)
        assert sub.user_remna_id == gift.remna_uuid
        assert store.panel[gift.remna_uuid].telegram_id == 222
        for actor in [store.buyer, recipient, store.buyer]:
            body, markup = await svc.card(actor, gift_id)
            assert sub.url in body and "&lt;name&gt;" in body
            assert "Оплатить" not in [b.text for row in markup.inline_keyboard for b in row]
        gift = await svc.get(recipient, gift_id)
        assert gift.recipient_seen_at
        outsider = await svc.users.create(
            UserDto(name="Other", telegram_id=333, referral_code=uuid4().hex)
        )
        with pytest.raises(GiftError):
            await svc.card(outsider, gift_id)


async def test_existing_paid_subscription_is_not_replaced_and_gift_has_own_link(store):
    async with store.service() as svc:
        receiver = await svc.users.create(
            UserDto(
                name="Receiver", telegram_id=222, referral_code=uuid4().hex, purchase_discount=25
            )
        )
        snapshot = PlanSnapshotDto.from_plan(plan(), 30)
        original = await svc.subscriptions.create(
            SubscriptionDto(
                user_remna_id=uuid4(),
                traffic_limit=0,
                device_limit=9,
                traffic_limit_strategy=snapshot.traffic_limit_strategy,
                expire_at=datetime_now() + timedelta(days=90),
                url="https://example.test/original",
                plan_snapshot=snapshot,
            ),
            receiver.id,
        )
        await svc.session.commit()
    gift_id, payment_id = await store.order()
    async with store.service() as svc:
        await svc.complete(payment_id)
        current = await svc.subscriptions.get_current(receiver.id)
        assert current.id == original.id and current.device_limit == 9
        body, _ = await svc.card(store.buyer, gift_id)
        assert original.url not in body
        assert (await svc.users.get_by_id(receiver.id)).purchase_discount == 25


async def test_concurrent_duplicate_callbacks_issue_exactly_once(store):
    gift_id, payment_id = await store.order()

    async def complete():
        async with store.service() as svc:
            await svc.complete(payment_id)

    await asyncio.gather(complete(), complete(), complete())
    store.remote.create_user.assert_awaited_once()
    async with store.service() as svc:
        gift = await svc.get(store.buyer, gift_id)
        count = await svc.session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.user_remna_id == gift.remna_uuid)
        )
        assert count == 1
        assert (
            await svc.session.scalar(
                select(func.count()).select_from(User).where(User.telegram_id == 222)
            )
            == 1
        )
        deadline = gift.expire_at
        await svc.complete(payment_id)
        assert (await svc.get(store.buyer, gift_id)).expire_at == deadline


async def test_crash_after_remote_create_recovers_same_uuid_and_expiry(store):
    gift_id, payment_id = await store.order()
    async with store.service() as svc:
        with patch.object(svc.session, "flush", side_effect=RuntimeError("simulated crash")):
            with pytest.raises(RuntimeError):
                await svc.complete(payment_id)
    assert len(store.panel) == 1
    remote_before = next(iter(store.panel.values()))
    async with store.service() as svc:
        await svc.recover()
        gift = await svc.get(store.buyer, gift_id)
        assert gift.issued_at and gift.expire_at == remote_before.expire_at
        assert gift.remna_uuid == remote_before.uuid
    store.remote.create_user.assert_awaited_once()


async def test_refund_disables_only_gift_and_duplicate_success_cannot_restore_it(store):
    gift_id, payment_id = await store.order()
    async with store.service() as svc:
        await svc.complete(payment_id)
        gift = await svc.get(store.buyer, gift_id)
        await svc.refund(payment_id)
        await svc.complete(payment_id)
        body, markup = await svc.card(store.buyer, gift_id)
        assert "Оплата возвращена" in body
        assert "https://example.test/sub" not in body
        assert not any(b.url for row in markup.inline_keyboard for b in row)
        store.remote.disable_user.assert_awaited_once_with(gift.remna_uuid)
        store.remote.create_user.assert_awaited_once()


async def test_recipient_who_cannot_receive_messages_keeps_paid_gift(store):
    gift_id, payment_id = await store.order()

    async def send(chat_id, *args, **kwargs):
        if chat_id == 222:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=222, text="Gift"),
                message="bot can't initiate conversation",
            )

    store.bot.send_message.side_effect = send
    async with store.service() as svc:
        await svc.complete(payment_id)
        gift = await svc.get(store.buyer, gift_id)
        assert gift.issued_at and gift.recipient_unreachable_at
        receiver = await svc.users.get_by_telegram_id(222)
        assert (await svc.list_for(receiver, received=True))[0][0].id == gift_id
        body, _ = await svc.card(receiver, gift_id)
        assert "https://example.test/sub" in body


async def test_checkout_permissions_catalog_and_payments_switch(store):
    async with store.service() as svc:
        with pytest.raises(GiftError):
            await svc.create(store.buyer, store.buyer.telegram_id, "Self", None)
        gift = await svc.create(store.buyer, 222, "Receiver", None)
        other = UserDto(id=987, telegram_id=987, name="Other")
        with pytest.raises(GiftError):
            await svc.quote(other, gift.id, plan().id, 30, Currency.RUB)
        store.catalog.get_active_plans.return_value = [plan()]
        store.catalog.get_active_plans.return_value[0].availability = PlanAvailability.ALLOWED
        assert await svc.available_plans() == []
        store.catalog.get_active_plans.return_value = [plan()]
        store.config.get.return_value.access.payments_allowed = False
        with pytest.raises(GiftError):
            await svc.quote(store.buyer, gift.id, plan().id, 30, Currency.RUB)


async def test_repeated_checkout_reuses_invoice(store):
    async with store.service() as svc:
        gift = await svc.create(store.buyer, 222, "Receiver", None)
        snapshot, price = await svc.quote(store.buyer, gift.id, plan().id, 30, Currency.RUB)
        gateway = SimpleNamespace(
            data=SimpleNamespace(
                type=PaymentGatewayType.YOOKASSA, currency=Currency.RUB, settings=None
            ),
            handle_create_payment=AsyncMock(
                return_value=SimpleNamespace(id=uuid4(), url="https://example.test/pay")
            ),
        )
        create = CreatePayment(
            UnitOfWorkImpl(svc.session),
            Mock(),
            svc.transactions,
            SimpleNamespace(system=AsyncMock(return_value=gateway)),
            SimpleNamespace(
                get_translator_by_locale=lambda _: SimpleNamespace(get=lambda *a, **k: "Invoice")
            ),
            svc,
        )
        data = CreatePaymentDto(
            snapshot, price, PurchaseType.NEW, PaymentGatewayType.YOOKASSA, gift.id
        )
        first = await create(store.buyer, data)
        second = await create(store.buyer, data)
        assert first.id == second.id and first.url == second.url
        gateway.handle_create_payment.assert_awaited_once()
        row = await svc.transaction(gift.id)
        assert row.user_id == store.buyer.id and row.gift_id == gift.id
        assert row.plan_snapshot["duration"] == 30


async def test_two_gifts_for_same_new_recipient_share_account_but_not_subscriptions(store):
    first_id, first_payment = await store.order()
    second_id, second_payment = await store.order()

    async def complete(payment):
        async with store.service() as svc:
            await svc.complete(payment)

    await asyncio.gather(complete(first_payment), complete(second_payment))
    async with store.service() as svc:
        recipient = await svc.users.get_by_telegram_id(222)
        first = await svc.get(recipient, first_id)
        second = await svc.get(recipient, second_id)
        assert first.remna_uuid != second.remna_uuid
        assert first.subscription_id != second.subscription_id
        assert len(await svc.subscriptions.get_all_by_user(recipient.id)) == 2
        assert (await svc.subscriptions.get_current(recipient.id)).id in (
            first.subscription_id,
            second.subscription_id,
        )
        assert (
            await svc.session.scalar(
                select(func.count()).select_from(User).where(User.telegram_id == 222)
            )
            == 1
        )


async def test_panel_sync_updates_only_gift_uuid(store):
    first_id, first_payment = await store.order()
    second_id, second_payment = await store.order()
    async with store.service() as svc:
        await svc.complete(first_payment)
        await svc.complete(second_payment)
        recipient = await svc.users.get_by_telegram_id(222)
        first = await svc.get(recipient, first_id)
        second = await svc.get(recipient, second_id)
        changed_remote = store.panel[second.remna_uuid]
        sync = SyncRemnaUser(
            UnitOfWorkImpl(svc.session), svc.users, svc.subscriptions, Mock(), Mock(), Mock()
        )
        sync._update_subscription = AsyncMock(return_value=True)
        with patch.object(RemnaSubscriptionDto, "from_remna_user", return_value=Mock()):
            await sync.system(SyncRemnaUserDto(changed_remote, creating=False))
        target = sync._update_subscription.call_args.args[0]
        assert target.id == second.subscription_id
        assert (await svc.subscriptions.get_current(recipient.id)).id == first.subscription_id


@pytest.mark.parametrize("wrong", ["user", "amount", "currency", "status", None])
async def test_stars_precheckout_checks_buyer_amount_currency_and_status(wrong):
    tx = SimpleNamespace(
        user_id=1,
        gateway_type=PaymentGatewayType.TELEGRAM_STARS,
        currency=Currency.XTR,
        pricing=SimpleNamespace(final_amount=Decimal(100)),
        status=TransactionStatus.PENDING,
    )
    user = TelegramUserDto(id=1, telegram_id=111, name="Buyer")
    query = SimpleNamespace(
        invoice_payload=str(uuid4()), currency="XTR", total_amount=100, answer=AsyncMock()
    )
    if wrong == "user":
        tx.user_id = 2
    if wrong == "amount":
        query.total_amount = 1
    if wrong == "currency":
        query.currency = "RUB"
    if wrong == "status":
        tx.status = TransactionStatus.COMPLETED
    await on_pre_checkout(
        query, user, SimpleNamespace(get_by_payment_id=AsyncMock(return_value=tx))
    )
    assert query.answer.call_args.kwargs["ok"] == (wrong is None)


async def test_owner_stars_gift_is_real_purchase_and_not_automatically_refunded():
    payment_id = uuid4()
    message = SimpleNamespace(
        successful_payment=SimpleNamespace(
            invoice_payload=str(payment_id),
            currency="XTR",
            total_amount=100,
            telegram_payment_charge_id="charge",
        )
    )
    tx = SimpleNamespace(
        user_id=1,
        gift_id=12,
        currency=Currency.XTR,
        pricing=SimpleNamespace(final_amount=Decimal(100)),
    )
    bot = SimpleNamespace(refund_star_payment=AsyncMock())
    process = SimpleNamespace(system=AsyncMock())
    await on_successful_payment(
        message,
        TelegramUserDto(
            id=1,
            telegram_id=111,
            name="Owner",
            role=Role.OWNER,
        ),
        bot,
        process,
        SimpleNamespace(get_by_payment_id=AsyncMock(return_value=tx)),
    )
    bot.refund_star_payment.assert_not_awaited()
    assert process.system.call_args.args[0].new_transaction_status == TransactionStatus.COMPLETED


def test_gift_identity_and_subscription_pass_real_remnawave_request_validation():
    identity = GiftRecipient(name="Receiver", telegram_id=222, gift_uuid=uuid4())
    snapshot = PlanSnapshotDto.from_plan(plan(), 30)
    subscription = SubscriptionDto(
        user_remna_id=identity.gift_uuid,
        traffic_limit=0,
        device_limit=5,
        traffic_limit_strategy=snapshot.traffic_limit_strategy,
        expire_at=datetime_now() + timedelta(days=30),
        url="",
        plan_snapshot=snapshot,
    )
    request = RemnawaveImpl._build_create_request(Mock(), identity, None, subscription)
    assert len(request.username) <= 36
    assert request.uuid == identity.gift_uuid and request.telegram_id == 222
    assert request.expire_at == subscription.expire_at


async def test_migration_round_trip_keeps_ordinary_payments(store):
    async with store.service() as svc:
        ordinary = await svc.transactions.create(
            TransactionDto(
                payment_id=uuid4(),
                user_id=store.buyer.id,
                status=TransactionStatus.COMPLETED,
                purchase_type=PurchaseType.NEW,
                gateway_type=PaymentGatewayType.YOOKASSA,
                currency=Currency.RUB,
                pricing=PriceDetailsDto.test(),
                plan_snapshot=PlanSnapshotDto.from_plan(plan(), 30),
            )
        )
        await svc.session.commit()
    migration = import_module("src.infrastructure.database.migrations.versions.0043_add_gifts")

    def migrate(connection):
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()

    async with store.engine.begin() as connection:
        await connection.run_sync(migrate)
    async with store.service() as svc:
        payment = await svc.transactions.get_by_payment_id(ordinary.payment_id)
        assert payment.status == TransactionStatus.COMPLETED and payment.gift_id is None
        assert payment.pricing == ordinary.pricing


@pytest.mark.parametrize("action", ["plan", "duration", "pay"])
async def test_gift_buttons_resolve_dependencies_through_dispatcher(action):
    class Dependencies(Provider):
        scope = Scope.APP
        gifts = from_context(provides=GiftService)
        gateways = from_context(provides=PaymentGatewayDao)
        create_payment = from_context(provides=CreatePayment)
        redis = from_context(provides=Redis)
        translator = from_context(provides=TranslatorRunner)

    user = TelegramUserDto(id=1, telegram_id=123, name="Buyer")
    snapshot = PlanSnapshotDto.from_plan(plan(), 30)
    pricing = PriceDetailsDto(
        original_amount=Decimal(300), discount_percent=0, final_amount=Decimal(300)
    )
    gifts = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                id=7, recipient_name="Recipient", recipient_telegram_id=222
            )
        ),
        transaction=AsyncMock(return_value=None),
        available_plans=AsyncMock(return_value=[plan()]),
        quote=AsyncMock(return_value=(snapshot, pricing)),
        card=AsyncMock(return_value=("Счёт готов", InlineKeyboardMarkup(inline_keyboard=[]))),
        session=SimpleNamespace(rollback=AsyncMock()),
    )
    gateway = SimpleNamespace(
        type=PaymentGatewayType.YOOKASSA, currency=Currency.RUB, settings=None
    )
    create_payment = AsyncMock()
    i18n = SimpleNamespace(
        get=Mock(
            side_effect=lambda key, **kw: {"Premium": "Премиум", "gateway-type": "ЮKassa"}[key]
        )
    )
    container = make_async_container(
        Dependencies(),
        context={
            GiftService: gifts,
            PaymentGatewayDao: SimpleNamespace(get_active=AsyncMock(return_value=[gateway])),
            CreatePayment: create_payment,
            Redis: SimpleNamespace(),
            TranslatorRunner: i18n,
        },
    )
    dispatcher = Dispatcher()
    dispatcher.callback_query.register(on_gift_callback, F.data.startswith("gift:"))
    setup_dishka(container, dispatcher)
    bot = Bot("123456:TEST_TOKEN")
    payload = {
        "plan": "gift:plan:7:5",
        "duration": "gift:duration:7:5:30",
        "pay": "gift:pay:7:5:30:YOOKASSA",
    }[action]
    update = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="gift-button",
            from_user=TelegramUser(id=123, is_bot=False, first_name="Buyer"),
            chat_instance="gift-test",
            data=payload,
            message=Message(message_id=1, date=0, chat=Chat(id=123, type="private")),
        ),
    )
    try:
        with (
            patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
            patch.object(Bot, "send_message", new_callable=AsyncMock) as send,
        ):
            await dispatcher.feed_update(
                bot, update, user=user, dialog_manager=SimpleNamespace()
            )
            answer.assert_awaited_once()
            send.assert_awaited_once()
            message = send.call_args.args[1]
            rows = send.call_args.kwargs["reply_markup"].inline_keyboard
            if action == "plan":
                assert "Премиум" in message and rows[0][0].callback_data == "gift:duration:7:5:30"
            elif action == "duration":
                assert "Recipient" in message and "222" in message
                assert rows[0][0].text == "ЮKassa · 300 ₽"
                assert rows[0][0].callback_data == "gift:pay:7:5:30:YOOKASSA"
            else:
                create_payment.assert_awaited_once()
                actor, checkout = create_payment.call_args.args
                assert actor == user and checkout.gift_id == 7
                assert checkout.plan_snapshot == snapshot and checkout.pricing == pricing
                assert checkout.gateway_type == PaymentGatewayType.YOOKASSA
                gifts.card.assert_awaited_once_with(user, 7)
            gifts.session.rollback.assert_not_awaited()
    finally:
        await container.close()
        await bot.session.close()
