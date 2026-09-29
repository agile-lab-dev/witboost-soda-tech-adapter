from __future__ import annotations

from typing import List, Tuple, Union

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError


class SodaPlatformSpecific(BaseModel):
    """
    Shape expected under `specific.<platform-key>` for a Soda workload component,
    e.g. `specific.databricks.{dataSourceName, outputPortIds}`.

    Extensibility by design: adding support for a new target platform means adding
    a new sibling key under `specific` (e.g. `specific.snowflake`) with this same
    shape, without changing existing platform branches. See docs/HLD.md
    "Extensibility by design".
    """

    dataSourceName: str
    outputPortIds: List[str]


def extract_soda_platform_specific(
    specific: dict,
) -> Union[Tuple[str, SodaPlatformSpecific], List[str]]:
    """
    Extract the `(platform_key, SodaPlatformSpecific)` pair from a Soda workload's
    `specific` dict.

    `specific` is expected to contain exactly one key naming the target platform
    (e.g. "databricks"), whose value holds `dataSourceName` and `outputPortIds`.
    The adapter dispatches on whichever platform key is present rather than
    assuming a fixed one, so new platforms only require a new sibling key.

    Args:
        specific: The `specific` dict of a Soda workload component.

    Returns:
        Tuple[str, SodaPlatformSpecific]: on success.
        List[str]: human-readable validation error messages on failure.
    """
    if not specific:
        return [
            "`specific` is empty: expected a single platform key (e.g. `databricks`) "
            "with `dataSourceName` and `outputPortIds`."
        ]

    if len(specific) != 1:
        return [
            "`specific` must contain exactly one platform key (e.g. `databricks`); found: "
            + ", ".join(sorted(specific.keys()))
        ]

    ((platform_key, platform_value),) = specific.items()

    if not isinstance(platform_value, dict):
        return [f"`specific.{platform_key}` must be an object with `dataSourceName` and `outputPortIds`."]

    try:
        return platform_key, SodaPlatformSpecific(**platform_value)
    except PydanticValidationError as ve:
        return [
            f"`specific.{platform_key}` is invalid: {err}"
            for err in ve.errors(include_url=False, include_context=False, include_input=False)
        ]
