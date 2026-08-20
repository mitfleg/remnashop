from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from httpx import AsyncClient, Timeout
from loguru import logger
from remnapy import RemnawaveSDK

from src.core.config import AppConfig
from src.infrastructure.services.remnawave_v3_compat import (
    remnawave_v3_request_hook,
    remnawave_v3_response_hook,
)


class RemnawaveProvider(Provider):
    scope = Scope.APP

    @provide
    async def get_remnawave(self, config: AppConfig) -> AsyncIterator[RemnawaveSDK]:
        logger.debug("Initializing RemnawaveSDK")

        headers = {}
        headers["Authorization"] = f"Bearer {config.remnawave.token.get_secret_value()}"
        headers["X-Api-Key"] = config.remnawave.caddy_token.get_secret_value()
        headers["CF-Access-Client-Id"] = config.remnawave.cf_client_id.get_secret_value()
        headers["CF-Access-Client-Secret"] = config.remnawave.cf_client_secret.get_secret_value()

        if not config.remnawave.is_external:
            headers["x-forwarded-proto"] = "https"
            headers["x-forwarded-for"] = "127.0.0.1"

        client = AsyncClient(
            base_url=f"{config.remnawave.url.get_secret_value()}/api",
            headers=headers,
            cookies=config.remnawave.cookies,
            verify=True,
            timeout=Timeout(connect=15.0, read=25.0, write=10.0, pool=5.0),
            event_hooks={
                "request": [remnawave_v3_request_hook],
                "response": [remnawave_v3_response_hook],
            },
        )

        try:
            yield RemnawaveSDK(client)
        finally:
            await client.aclose()
            logger.debug("RemnawaveSDK AsyncClient closed")
