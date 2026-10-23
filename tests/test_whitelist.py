import copy
import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update
from aiogram.types import User as TelegramUser
from aiogram_dialog import Dialog, Window, setup_dialogs
from aiogram_dialog.widgets.text import Const
from dishka import Provider, Scope, from_context, make_async_container
from dishka.integrations.aiogram import setup_dishka
from pydantic import SecretStr

from src.application.dto import PlanSnapshotDto, SubscriptionDto, TelegramUserDto
from src.core.config import AppConfig
from src.core.constants import CONFIG_KEY, CONTAINER_KEY, USER_KEY
from src.core.utils.time import datetime_now

with patch.object(
    AppConfig,
    "get",
    return_value=SimpleNamespace(redis=SimpleNamespace(dsn="redis://localhost/15")),
):
    from src.infrastructure.services.cabinet_whitelist import (
        API_PATH,
        CabinetError,
        CabinetWhitelist,
        sign_request,
    )
    from src.infrastructure.services.remnawave import RemnawaveImpl
    from src.telegram.routers.menu.dialog import menu
    from src.telegram.routers.whitelist import dialog, getter
    from src.telegram.states import MainMenu


def identity():
    return TelegramUserDto(id=7, telegram_id=123, name="Buyer")


def offer():
    return {
        "available": True,
        "priceKopecks": 24900,
        "durationDays": 30,
        "planId": "plan",
        "providers": [{"id": "YOOKASSA", "label": "ЮKassa"}],
    }


async def test_transport_signs_identity_and_uses_cabinet_quote_without_email():
    client = CabinetWhitelist(
        SimpleNamespace(
            web_cabinet_url="https://cabinet.test", cabinet_api_secret=SecretStr("s" * 32)
        )
    )
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"localPaymentId": "p1", "confirmationUrl": "https://pay.test"}
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = Mock(post=Mock(return_value=context))
    factory = AsyncMock()
    factory.__aenter__.return_value = session
    with patch("aiohttp.ClientSession", return_value=factory):
        result = await client.checkout(identity(), offer(), "YOOKASSA", str(uuid4()))
    assert result["localPaymentId"] == "p1"
    args, kwargs = session.post.call_args
    body = kwargs["data"].decode()
    payload = json.loads(body)
    assert payload["telegramId"] == "123" and payload["sourceUserId"] == 7
    assert payload["priceKopecks"] == 24900 and "email" not in payload
    headers = kwargs["headers"]
    signed = f"{headers['X-Bot-Timestamp']}\nPOST\n{API_PATH}\n{body}".encode()
    assert headers["X-Bot-Signature"] == hmac.new(b"s" * 32, signed, hashlib.sha256).hexdigest()
    assert (
        args[0] == "https://cabinet.test/api/bot/whitelist" and kwargs["allow_redirects"] is False
    )
    assert (
        sign_request("s" * 32, headers["X-Bot-Timestamp"], body.replace('"123"', '"999"'))
        != headers["X-Bot-Signature"]
    )


@pytest.mark.parametrize("fails", [False, True])
async def test_renewal_preserves_squads_and_survives_cabinet_outage(fails):
    cabinet = SimpleNamespace(
        enabled=True,
        stage_squad_sync=AsyncMock(return_value="queued"),
        deliver_squad_sync=AsyncMock(return_value={"activeInternalSquads": []}),
    )
    sdk = SimpleNamespace(
        users=SimpleNamespace(
            update_user=AsyncMock(
                return_value=SimpleNamespace(
                    username="test", uuid=uuid4(), telegram_id=123, active_internal_squads=[]
                )
            )
        )
    )
    adapter = RemnawaveImpl(sdk, cabinet)
    snapshot = PlanSnapshotDto.test()
    snapshot.internal_squads = [uuid4()]
    subscription = SubscriptionDto(
        user_remna_id=uuid4(),
        traffic_limit=5,
        device_limit=3,
        traffic_limit_strategy=snapshot.traffic_limit_strategy,
        expire_at=datetime_now() + timedelta(days=30),
        url="https://sub.test",
        plan_snapshot=snapshot,
        internal_squads=[*snapshot.internal_squads, uuid4()],
    )
    if fails:
        cabinet.deliver_squad_sync.side_effect = CabinetError("unavailable")
    await adapter.update_user(identity(), subscription.user_remna_id, subscription=subscription)
    request = sdk.users.update_user.call_args.args[0]
    # Exact serialization flags used by remnapy's PydanticBody transport.
    body = request.model_dump(exclude_unset=True, by_alias=True, mode="json")
    assert "activeInternalSquads" not in body
    assert body["expireAt"] == subscription.expire_at.isoformat().replace("+00:00", "Z")
    assert body["hwidDeviceLimit"] == 3 and body["trafficLimitBytes"] == 5 * 1024**3
    assert cabinet.stage_squad_sync.call_args.kwargs["planSquads"] == [
        str(snapshot.internal_squads[0])
    ]


async def test_paid_waiting_and_paused_screens_never_offer_a_second_purchase():
    for payment in [
        {"status": "SUCCEEDED", "provisioned": False},
        {"status": "SUCCEEDED", "provisioned": True, "paused": True},
    ]:
        manager = SimpleNamespace(
            dialog_data={
                "offer": offer(),
                "invoice": {"localPaymentId": "p1", "confirmationUrl": "https://pay.test"},
                "payment": payment,
            }
        )
        data = await getter(manager)
        assert data["can_buy"] is False and data["can_pay"] is False
        if payment.get("paused"):
            assert "сохранены" in data["text"]


async def test_main_buttons_have_requested_style_and_visibility():
    manager = SimpleNamespace(
        is_preview=lambda: False,
        middleware_data={
            CONTAINER_KEY: SimpleNamespace(
                get=AsyncMock(return_value=SimpleNamespace(get=lambda key, **kw: key))
            )
        },
    )
    buy = menu.find("whitelist")
    assert buy and menu.find("gift_history") is None
    assert await buy.render_keyboard({"whitelist_available": False}, manager) == []
    rows = await buy.render_keyboard(
        {"whitelist_available": True, "whitelist_price": "249 ₽"}, manager
    )
    assert rows[0][0].style == "primary" and "249 ₽" in rows[0][0].text
    gifts = await menu.find("gift_new").render_keyboard({}, manager)
    assert gifts[0][0].style is None


async def test_native_dialog_buys_without_email_and_hides_button_after_activation():  # noqa: C901
    class TelegramSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.last_menu = None
            self.calls = []

        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if method.__api_method__ == "getMe":
                return TelegramUser(id=123456, is_bot=True, first_name="Bot", username="test_bot")
            if method.__api_method__ in {
                "sendMessage",
                "editMessageText",
                "editMessageReplyMarkup",
            }:
                message = Message(
                    message_id=getattr(method, "message_id", None) or 10,
                    date=0,
                    chat=Chat(id=123, type="private"),
                    from_user=TelegramUser(id=123456, is_bot=True, first_name="Bot"),
                    text=getattr(method, "text", None),
                    reply_markup=method.reply_markup
                    if isinstance(method.reply_markup, InlineKeyboardMarkup)
                    else None,
                )
                if message.reply_markup:
                    self.last_menu = message
                return message
            return True

    cabinet = SimpleNamespace(
        offer=AsyncMock(return_value=offer()),
        checkout=AsyncMock(
            return_value={
                "localPaymentId": "p1",
                "amountKopecks": 24900,
                "confirmationUrl": "https://pay.test",
            }
        ),
        request=AsyncMock(return_value={"status": "SUCCEEDED", "provisioned": True}),
    )

    class Dependencies(Provider):
        scope = Scope.APP
        cabinet = from_context(provides=CabinetWhitelist)

    container = make_async_container(Dependencies(), context={CabinetWhitelist: cabinet})
    session = TelegramSession()
    bot = Bot("123456:TEST_TOKEN", session=session)
    dispatcher = Dispatcher()

    async def start(message, dialog_manager):
        await dialog_manager.start(MainMenu.MAIN)

    async def main_getter(**kwargs):
        return {
            "whitelist_available": cabinet.offer.return_value["available"],
            "whitelist_price": "249 ₽",
        }

    dispatcher.message.register(start, Command("start"))
    main = Dialog(
        Window(
            Const("Главное меню"),
            copy.deepcopy(menu.find("whitelist")),
            state=MainMenu.MAIN,
            getter=main_getter,
        )
    )
    dispatcher.include_routers(main, copy.deepcopy(dialog))
    setup_dialogs(dispatcher)
    setup_dishka(container, dispatcher)
    sender = TelegramUser(id=123, is_bot=False, first_name="Buyer")
    counter = 0

    async def feed(**event):
        nonlocal counter
        counter += 1
        await dispatcher.feed_update(
            bot,
            Update(update_id=counter, **event),
            **{
                USER_KEY: identity(),
                CONFIG_KEY: SimpleNamespace(
                    bot=SimpleNamespace(use_banners=False), banners_dir=Path("assets/banners")
                ),
            },
        )

    async def click(label):
        button = next(
            b
            for row in session.last_menu.reply_markup.inline_keyboard
            for b in row
            if label in b.text
        )
        await feed(
            callback_query=CallbackQuery(
                id=f"c{counter}",
                from_user=sender,
                chat_instance="test",
                data=button.callback_data,
                message=session.last_menu,
            )
        )

    try:
        await feed(
            message=Message(
                message_id=1,
                date=0,
                chat=Chat(id=123, type="private"),
                from_user=sender,
                text="/start",
            )
        )
        await click("Купить белые списки")
        assert "30 дней" in session.last_menu.text and "INCY" in session.last_menu.text
        await click("ЮKassa")
        assert cabinet.checkout.await_count == 1
        assert any(
            b.url == "https://pay.test"
            for row in session.last_menu.reply_markup.inline_keyboard
            for b in row
        )
        await click("Проверить оплату")
        assert "подключены" in session.last_menu.text
        cabinet.offer.return_value = {"available": False}
        await click("В главное меню")
        assert not session.last_menu.reply_markup or not any(
            "белые списки" in b.text
            for row in session.last_menu.reply_markup.inline_keyboard
            for b in row
        )
    finally:
        await container.close()
        await bot.session.close()


async def test_squad_queue_is_persisted_before_update_and_acknowledged_only_after_delivery():
    redis = SimpleNamespace(pipeline=Mock(), eval=AsyncMock())
    pipe = AsyncMock()
    pipe.__aenter__.return_value = pipe
    pipe.hset = Mock()
    pipe.zadd = Mock()
    redis.pipeline.return_value = pipe
    client = CabinetWhitelist(
        SimpleNamespace(
            web_cabinet_url="https://cabinet.test", cabinet_api_secret=SecretStr("s" * 32)
        ),
        redis,
    )
    body = await client.stage_squad_sync(identity(), expireAt="2026-10-24T00:00:00Z")
    pipe.execute.assert_awaited_once()
    assert json.loads(body)["sourceUserId"] == 7
    with patch.object(client, "_send", side_effect=CabinetError("outage")):
        with pytest.raises(CabinetError):
            await client.deliver_squad_sync(body)
    redis.eval.assert_not_awaited()
    with patch.object(client, "_send", return_value={"activeInternalSquads": []}):
        await client.deliver_squad_sync(body)
    assert redis.eval.call_args.args[-2:] == ("7", body)


async def test_background_worker_retries_a_saved_squad_update_after_outage():
    body = json.dumps({"action": "sync-squads", "sourceUserId": 7})
    redis = SimpleNamespace(
        zrangebyscore=AsyncMock(return_value=["7"]),
        hget=AsyncMock(return_value=body),
        eval=AsyncMock(),
        zadd=AsyncMock(),
        zrem=AsyncMock(),
    )
    client = CabinetWhitelist(
        SimpleNamespace(
            web_cabinet_url="https://cabinet.test", cabinet_api_secret=SecretStr("s" * 32)
        ),
        redis,
    )
    with patch.object(client, "_send", side_effect=CabinetError("outage")):
        await client.reconcile_squads()
    redis.eval.assert_not_awaited()
    redis.zadd.assert_awaited_once()
    with patch.object(client, "_send", return_value={"activeInternalSquads": []}):
        await client.reconcile_squads()
    redis.eval.assert_awaited_once()
