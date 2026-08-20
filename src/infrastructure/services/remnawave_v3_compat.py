import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode
from uuid import UUID

from httpx import ByteStream, Request, Response

MAP_PATH = Path(
    os.getenv(
        "REMNAWAVE_V3_USER_MAP",
        "/opt/remnashop/assets/remnawave-v3-user-map.json",
    )
)
_UUID_SEGMENT = re.compile(r"(?<=/)([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})(?=/|$)")
_map_mtime_ns: int | None = None
_legacy_to_id: dict[str, int] = {}
_preferred_legacy_by_id: dict[str, str] = {}
_placeholder_user_ids: set[int] = set()


def _read_user_id_set(env_name: str) -> set[int]:
    return {
        int(item.strip())
        for item in os.getenv(env_name, "").split(",")
        if item.strip()
    }


HWID_LIMIT_EXEMPT_USER_IDS = _read_user_id_set("REMNAWAVE_HWID_LIMIT_EXEMPT_USER_IDS")


def _load_map() -> None:
    global _map_mtime_ns, _legacy_to_id, _preferred_legacy_by_id, _placeholder_user_ids

    if not MAP_PATH.exists():
        _map_mtime_ns = None
        _legacy_to_id = {}
        _preferred_legacy_by_id = {}
        _placeholder_user_ids = set()
        return

    stat = MAP_PATH.stat()
    if _map_mtime_ns == stat.st_mtime_ns:
        return

    payload = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    _legacy_to_id = {
        str(key).lower(): int(value) for key, value in payload["legacyToId"].items()
    }
    _preferred_legacy_by_id = {
        str(key): str(value).lower()
        for key, value in payload["preferredLegacyById"].items()
    }
    _placeholder_user_ids = {
        int(value) for value in payload.get("placeholderUserIds", [])
    }
    _map_mtime_ns = stat.st_mtime_ns


def resolve_user_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid Remnawave user ID")
    if isinstance(value, int):
        return value

    _load_map()
    text = str(value).lower()
    mapped = _legacy_to_id.get(text)
    if mapped is not None:
        return mapped

    parsed = UUID(text)
    if 0 < parsed.int <= 2**63 - 1:
        return parsed.int
    raise KeyError(f"No Remnawave v3 numeric ID mapping for legacy UUID '{text}'")


def _legacy_for_id(user_id: int) -> str:
    _load_map()
    return _preferred_legacy_by_id.get(str(user_id), str(UUID(int=user_id)))


def _rewrite_user_uuid_segments(path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group(1)
        try:
            return str(resolve_user_id(value))
        except (KeyError, ValueError):
            return value

    return _UUID_SEGMENT.sub(replace, path)


def _rewrite_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if key == "userUuid":
                rewritten["userId"] = resolve_user_id(item)
            elif key == "userUuids":
                rewritten["userIds"] = [resolve_user_id(entry) for entry in item]
            else:
                rewritten[key] = _rewrite_json_value(item)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_json_value(item) for item in value]
    return value


def _set_request_json(request: Request, payload: Any) -> None:
    content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request._content = content
    request.stream = ByteStream(content)
    request.headers["content-length"] = str(len(content))


def _set_response_json(response: Response, payload: Any) -> None:
    content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response._content = content
    response.headers.pop("content-encoding", None)
    response.headers.pop("transfer-encoding", None)
    response.headers["content-type"] = "application/json"
    response.headers["content-length"] = str(len(content))


def _inject_legacy_identifiers(value: Any) -> None:
    if isinstance(value, dict):
        user_id = value.get("id")
        if (
            isinstance(user_id, int)
            and not isinstance(user_id, bool)
            and "username" in value
            and "shortUuid" in value
            and "uuid" not in value
        ):
            value["uuid"] = _legacy_for_id(user_id)

        related_user_id = value.get("userId")
        is_user_related_record = "hwid" in value or "requestAt" in value
        if (
            isinstance(related_user_id, int)
            and not isinstance(related_user_id, bool)
            and is_user_related_record
            and "userUuid" not in value
        ):
            value["userUuid"] = _legacy_for_id(related_user_id)

        for item in value.values():
            _inject_legacy_identifiers(item)
    elif isinstance(value, list):
        for item in value:
            _inject_legacy_identifiers(item)


def prepare_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add legacy identifiers after the original v3 webhook signature is verified."""
    _inject_legacy_identifiers(payload)
    return payload


async def remnawave_v3_request_hook(request: Request) -> None:
    path = request.url.path
    query = list(parse_qsl(request.url.query.decode("utf-8"), keep_blank_values=True))

    lookup_patterns = (
        (r"/users/by-telegram-id/([^/]+)$", "telegramId"),
        (r"/users/by-email/([^/]+)$", "email"),
        (r"/users/by-tag/([^/]+)$", "tag"),
    )
    for pattern, query_name in lookup_patterns:
        match = re.search(pattern, path)
        if match:
            path = re.sub(pattern, "/users/stream", path)
            query.append((query_name, unquote(match.group(1))))
            request.extensions["remnashop_v3_stream_lookup"] = True
            break

    path = re.sub(r"/users/by-id/([^/]+)$", r"/users/\1", path)
    path = path.replace("/subscriptions/by-uuid/", "/subscriptions/by-id/")
    path = path.replace("/ip-control/drop-connections", "/connections/drop")
    path = _rewrite_user_uuid_segments(path)
    request.url = request.url.copy_with(path=path, query=urlencode(query).encode("utf-8"))

    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        payload = _rewrite_json_value(payload)
        if request.url.path.endswith("/api/users"):
            if request.method == "PATCH" and "uuid" in payload:
                payload["id"] = resolve_user_id(payload.pop("uuid"))
            elif request.method == "POST":
                payload.pop("uuid", None)
            if (
                request.method == "PATCH"
                and payload.get("id") in HWID_LIMIT_EXEMPT_USER_IDS
            ):
                payload["hwidDeviceLimit"] = 0
        _set_request_json(request, payload)


async def remnawave_v3_response_hook(response: Response) -> None:
    path = response.request.url.path

    missing_hwid_match = re.search(r"/api/hwid/devices/(\d+)$", path)
    if response.status_code == 404 and missing_hwid_match:
        _load_map()
        if int(missing_hwid_match.group(1)) in _placeholder_user_ids:
            await response.aread()
            response.status_code = 200
            _set_response_json(response, {"response": {"total": 0, "devices": []}})
            return

    if response.status_code == 204 and response.request.method == "DELETE" and re.search(
        r"/api/users/\d+$", path
    ):
        response.status_code = 200
        _set_response_json(response, {"response": {"isDeleted": True}})
        return

    if response.status_code == 202 and path.endswith("/api/connections/drop"):
        response.status_code = 200
        _set_response_json(response, {"response": {"eventSent": True}})
        return

    if "application/json" not in response.headers.get("content-type", ""):
        return

    await response.aread()
    if not response.content:
        return
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return

    _inject_legacy_identifiers(payload)
    if response.request.extensions.get("remnashop_v3_stream_lookup"):
        inner = payload.get("response", {})
        payload["response"] = inner.get("users", [])
    _set_response_json(response, payload)
