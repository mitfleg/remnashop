from packaging.version import Version

from src.application.services import safe_parse_version


def test_custom_build_version_is_valid_and_tracks_next_upstream_release() -> None:
    custom = safe_parse_version("0.8.2+custom.08dff7e")

    assert custom is not None
    assert custom > Version("0.8.2")
    assert custom < Version("0.8.3")


def test_invalid_build_version_is_rejected_without_exception() -> None:
    assert safe_parse_version("custom") is None
