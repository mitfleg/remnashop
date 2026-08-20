from typing import Optional

from src.application.dto import PlanDto


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
