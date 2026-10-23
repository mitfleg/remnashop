"""Authenticated transport to Cabinet; prices and payment grants stay in Cabinet."""

import hashlib
import hmac
import json
import time
from typing import Any

import aiohttp
from loguru import logger
from redis.asyncio import Redis

from src.application.dto import UserDto
from src.core.config import AppConfig

API_PATH = "/api/bot/whitelist"


class CabinetError(Exception):
    pass


def sign_request(secret: str, timestamp: str, body: str) -> str:
    payload = f"{timestamp}\nPOST\n{API_PATH}\n{body}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class CabinetWhitelist:
    def __init__(self, config: AppConfig, redis: Redis | None = None) -> None:
        self.redis = redis
        self.base_url = config.web_cabinet_url.rstrip("/")
        self.secret = (
            config.cabinet_api_secret.get_secret_value() if config.cabinet_api_secret else ""
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.secret)

    async def request(
        self, user: UserDto, action: str, *, timeout: float = 30, **payload: Any
    ) -> dict[str, Any]:
        data = {"action": action, "sourceUserId": user.id, **payload}
        if user.telegram_id:
            data["telegramId"] = str(user.telegram_id)
        return await self._send(data, timeout=timeout)

    async def _send(self, data: dict[str, Any], *, timeout: float = 30) -> dict[str, Any]:
        if not self.enabled:
            raise CabinetError("Покупка белых списков пока недоступна.")
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Bot-Timestamp": timestamp,
            "X-Bot-Signature": sign_request(self.secret, timestamp, body),
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(
                    self.base_url + API_PATH,
                    data=body.encode(),
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    result = await response.json()
                    if not isinstance(result, dict):
                        raise CabinetError("Некорректный ответ кабинета. Попробуйте позже.")
                    if response.status != 200:
                        message = (
                            result.get("error")
                            if response.status in (400, 403, 404, 409, 429)
                            else None
                        )
                        raise CabinetError(
                            message or "Кабинет временно недоступен. Попробуйте позже."
                        )
                    return result
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise CabinetError(
                "Не удалось проверить покупку. Повторите попытку — новый счёт не требуется."
            ) from exc

    async def offer(self, user: UserDto, *, timeout: float = 10) -> dict[str, Any]:
        return await self.request(user, "offer", timeout=timeout)

    async def checkout(
        self, user: UserDto, offer: dict[str, Any], provider: str, checkout_key: str
    ) -> dict[str, Any]:
        return await self.request(
            user,
            "checkout",
            timeout=45,
            provider=provider,
            idempotencyKey=checkout_key,
            priceKopecks=offer["priceKopecks"],
            planId=offer["planId"],
        )

    async def stage_squad_sync(self, user: UserDto, **payload: Any) -> str:
        if self.redis is None:
            raise CabinetError("Очередь синхронизации не настроена")
        data = {"action": "sync-squads", "sourceUserId": user.id, **payload}
        if user.telegram_id:
            data["telegramId"] = str(user.telegram_id)
        body = json.dumps(data, separators=(",", ":"))
        # Durable before the primary update. The expiry guard in Cabinet rejects a
        # queued update if the primary operation failed or a newer renewal replaced it.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(SYNC_DATA, str(user.id), body)
            pipe.zadd(SYNC_DUE, {str(user.id): time.time() + 60})
            await pipe.execute()
        return body

    async def deliver_squad_sync(self, body: str) -> dict[str, Any]:
        data = json.loads(body)
        result = await self._send(data, timeout=5)
        squads = result.get("activeInternalSquads")
        if not isinstance(squads, list) or not all(isinstance(item, str) for item in squads):
            raise CabinetError("Не удалось проверить сквады после обновления")
        if self.redis is not None:
            # redis-py's shared stubs include the synchronous return type.
            await self.redis.eval(  # type: ignore[misc]
                ACK_SYNC, 2, SYNC_DATA, SYNC_DUE, str(data["sourceUserId"]), body
            )
        return result

    async def reconcile_squads(self) -> None:
        if not self.enabled or self.redis is None:
            return
        for user_id in await self.redis.zrangebyscore(SYNC_DUE, 0, time.time(), start=0, num=10):
            body = await self.redis.hget(SYNC_DATA, user_id)  # type: ignore[misc]
            if not body:
                await self.redis.zrem(SYNC_DUE, user_id)
                continue
            try:
                await self.deliver_squad_sync(body.decode() if isinstance(body, bytes) else body)
            except Exception:
                # Retry later; logging must not contain the signed request or secret.
                logger.warning("Cabinet squad synchronization deferred")
                await self.redis.zadd(SYNC_DUE, {user_id: time.time() + 60})


SYNC_DATA = "cabinet:whitelist:sync:data"
SYNC_DUE = "cabinet:whitelist:sync:due"
ACK_SYNC = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
  redis.call('HDEL', KEYS[1], ARGV[1])
  redis.call('ZREM', KEYS[2], ARGV[1])
  return 1
end
return 0
"""
