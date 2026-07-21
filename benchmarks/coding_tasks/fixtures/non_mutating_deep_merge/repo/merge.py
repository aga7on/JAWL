from typing import Any


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override values into a shallow copy of base."""

    result = base.copy()
    result.update(override)
    return result
