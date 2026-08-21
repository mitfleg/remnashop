from datetime import datetime
from typing import Optional, Protocol, Sequence, TypeVar

from src.application.dto import PlanDto


class ActivityTrackedDevice(Protocol):
    hwid: str
    created_at: datetime
    updated_at: datetime


DeviceT = TypeVar("DeviceT", bound=ActivityTrackedDevice)


def resolve_initial_device_limit(plan: PlanDto, preferred: Optional[int] = None) -> int:
    if preferred is None or preferred < plan.device_limit or preferred > plan.max_device_limit:
        preferred = plan.device_limit
    return plan.resolve_device_limit(preferred)


def parse_device_limit_input(value: Optional[str], plan: PlanDto) -> int:
    if value is None:
        raise ValueError("Device limit is empty")

    try:
        selected = int(value.strip())
    except ValueError:
        raise ValueError(f"Invalid device limit: '{value}'")

    return plan.resolve_device_limit(selected)


def select_excess_devices(devices: Sequence[DeviceT], device_limit: int) -> list[DeviceT]:
    if device_limit < 1:
        return []

    excess_count = len(devices) - device_limit
    if excess_count <= 0:
        return []

    return sorted(
        devices,
        key=lambda device: (device.updated_at, device.created_at, device.hwid),
    )[:excess_count]
