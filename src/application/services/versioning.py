from typing import Optional

from packaging.version import InvalidVersion, Version


def safe_parse_version(value: str) -> Optional[Version]:
    try:
        return Version(value)
    except InvalidVersion:
        return None
