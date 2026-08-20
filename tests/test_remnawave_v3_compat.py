import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import UUID

import pytest
from httpx import Request, Response
from remnapy.models.hwid import GetUserHwidDevicesResponseDto
from remnapy.models.subscription_request_history import (
    GetAllSubscriptionRequestHistoryResponseDto,
)

module_path = (
    Path(__file__).parents[1]
    / "src"
    / "infrastructure"
    / "services"
    / "remnawave_v3_compat.py"
)
spec = spec_from_file_location("remnawave_v3_compat", module_path)
assert spec and spec.loader
compat = module_from_spec(spec)
spec.loader.exec_module(compat)

LEGACY_UUID = "00000000-0000-0000-0000-00000000007b"


@pytest.fixture
def user_map(tmp_path, monkeypatch):
    path = tmp_path / "user-map.json"
    path.write_text(
        json.dumps(
            {
                "legacyToId": {LEGACY_UUID: 123},
                "preferredLegacyById": {"123": LEGACY_UUID},
                "placeholderUserIds": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(compat, "MAP_PATH", path)
    monkeypatch.setattr(compat, "_map_mtime_ns", None)
    return path


@pytest.mark.asyncio
async def test_request_hook_rewrites_legacy_update_to_numeric_id(user_map) -> None:
    request = Request(
        "PATCH",
        "https://panel.example/api/users",
        json={"uuid": LEGACY_UUID, "hwidDeviceLimit": 8},
    )

    await compat.remnawave_v3_request_hook(request)

    assert json.loads(request.content) == {"id": 123, "hwidDeviceLimit": 8}


@pytest.mark.asyncio
async def test_response_hook_restores_legacy_identifiers(user_map) -> None:
    request = Request("GET", "https://panel.example/api/users/123")
    response = Response(
        200,
        request=request,
        json={
            "response": {
                "id": 123,
                "username": "rs_123",
                "shortUuid": "short",
                "devices": [{"userId": 123, "hwid": "device"}],
            }
        },
    )

    await compat.remnawave_v3_response_hook(response)

    payload = response.json()["response"]
    assert payload["uuid"] == LEGACY_UUID
    assert payload["devices"][0]["userUuid"] == LEGACY_UUID
    assert UUID(payload["uuid"]) == UUID(LEGACY_UUID)


@pytest.mark.asyncio
async def test_response_hook_keeps_original_remnapy_models_compatible(user_map) -> None:
    timestamp = "2026-08-20T12:00:00Z"

    hwid_request = Request("GET", "https://panel.example/api/hwid/devices/123")
    hwid_response = Response(
        200,
        request=hwid_request,
        json={
            "response": {
                "total": 1,
                "devices": [
                    {
                        "userId": 123,
                        "hwid": "device",
                        "createdAt": timestamp,
                        "updatedAt": timestamp,
                    }
                ],
            }
        },
    )
    await compat.remnawave_v3_response_hook(hwid_response)
    parsed_hwid = GetUserHwidDevicesResponseDto.model_validate(
        hwid_response.json()["response"]
    )

    history_request = Request("GET", "https://panel.example/api/subscription-requests")
    history_response = Response(
        200,
        request=history_request,
        json={
            "response": {
                "total": 1,
                "records": [
                    {
                        "id": 1,
                        "userId": 123,
                        "requestAt": timestamp,
                        "requestIp": "127.0.0.1",
                        "userAgent": "Happ/1.0",
                    }
                ],
            }
        },
    )
    await compat.remnawave_v3_response_hook(history_response)
    parsed_history = GetAllSubscriptionRequestHistoryResponseDto.model_validate(
        history_response.json()["response"]
    )

    assert parsed_hwid.devices[0].user_uuid == UUID(LEGACY_UUID)
    assert parsed_history.records[0].user_uuid == UUID(LEGACY_UUID)
