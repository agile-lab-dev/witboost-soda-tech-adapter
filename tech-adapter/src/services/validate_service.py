from __future__ import annotations

from typing import List

import yaml

from src.models.api_models import ValidationError as ApiValidationError
from src.models.api_models import ValidationResult
from src.models.constants import SODA_TECH_ADAPTER_INFRASTRUCTURE_TEMPLATE_ID
from src.models.data_product_descriptor import DataProduct, OutputPort, Workload
from src.models.soda_specific import extract_soda_platform_specific
from src.services.output_port_metadata import resolve_output_port_table_metadata

# Real Soda Contract Language check types (top-level YAML key of a single
# check entry), confirmed against https://docs.soda.io/reference/contract-language-reference.
# This is a fast-fail syntax check only; Soda Cloud performs definitive
# contract YAML validation at publish time (see docs/technical-details.md).
_RECOGNIZED_CONTRACT_CHECK_TYPES = (
    "schema",
    "row_count",
    "freshness",
    "missing",
    "invalid",
    "duplicate",
    "aggregate",
    "metric",
    "failed_rows",
    "group_by",
    "reconciliation",
)

_REQUIRED_TABLE_METADATA_FIELDS = ("catalog", "schema", "table")


def validate_soda_workload(data_product: DataProduct, component_id: str) -> ValidationResult:
    """
    Validate a Soda workload component descriptor before any Soda Cloud API call
    is made. Implements the checks documented in docs/technical-details.md
    (Validation section).

    Args:
        data_product: The parsed Data Product descriptor.
        component_id: The id of the component to validate (`componentIdToProvision`).

    Returns:
        ValidationResult: `valid=True` with no error, or `valid=False` with the
        list of validation error messages.
    """
    component = data_product.get_component_by_id(component_id)

    if component is None:
        return _invalid([f"Component '{component_id}' not found in the Data Product descriptor."])

    is_soda_workload = (
        isinstance(component, Workload)
        and component.infrastructureTemplateId == SODA_TECH_ADAPTER_INFRASTRUCTURE_TEMPLATE_ID
    )
    if not is_soda_workload:
        return _invalid(
            [
                f"Component '{component_id}' is not a Soda workload (expected kind "
                f"'workload' with infrastructureTemplateId "
                f"'{SODA_TECH_ADAPTER_INFRASTRUCTURE_TEMPLATE_ID}')."
            ]
        )

    extraction = extract_soda_platform_specific(component.specific)
    if isinstance(extraction, list):
        return _invalid(extraction)

    _platform_key, soda_specific = extraction

    errors: List[str] = []

    if not soda_specific.dataSourceName.strip():
        errors.append("`dataSourceName` must be a non-empty string.")

    if not soda_specific.outputPortIds:
        errors.append("`outputPortIds` must contain at least one Output Port id.")

    for output_port_id in soda_specific.outputPortIds:
        errors.extend(_validate_output_port(data_product, output_port_id))

    if errors:
        return _invalid(errors)

    return ValidationResult(valid=True)


def _validate_output_port(data_product: DataProduct, output_port_id: str) -> List[str]:
    output_port_component = data_product.get_component_by_id(output_port_id)

    if output_port_component is None:
        return [
            f"Output Port '{output_port_id}' referenced by the Soda workload was not "
            f"found in the Data Product descriptor."
        ]

    if not isinstance(output_port_component, OutputPort):
        return [
            f"Component '{output_port_id}' referenced by the Soda workload is not an "
            f"Output Port (kind='{output_port_component.kind}')."
        ]

    return _validate_table_metadata(output_port_component) + _validate_data_contract(output_port_component)


def _validate_table_metadata(output_port: OutputPort) -> List[str]:
    resolved = resolve_output_port_table_metadata(output_port)._asdict()
    missing = [field for field in _REQUIRED_TABLE_METADATA_FIELDS if not resolved.get(field)]
    if missing:
        return [
            f"Output Port '{output_port.id}' is missing required table metadata: {', '.join(missing)} "
            f"(expected either 'catalogNameOP'/'schemaNameOP'/'viewNameOP' or "
            f"'catalogName'/'schemaName'/'tableName' in `specific`)."
        ]
    return []


def _validate_data_contract(output_port: OutputPort) -> List[str]:
    errors: List[str] = []
    quality_checks = output_port.dataContract.quality or []

    if not quality_checks:
        errors.append(
            f"Output Port '{output_port.id}' has no data quality checks defined under `dataContract.quality`."
        )
        return errors

    for index, quality_check in enumerate(quality_checks):
        check_yaml = quality_check.check.strip()
        prefix = f"Output Port '{output_port.id}': `dataContract.quality[{index}]`"
        if not check_yaml:
            errors.append(f"{prefix}.check is empty.")
            continue

        try:
            parsed = yaml.safe_load(check_yaml)
        except yaml.YAMLError as ex:
            errors.append(f"{prefix}.check is not valid YAML: {ex}.")
            continue

        if not isinstance(parsed, dict) or len(parsed) != 1:
            errors.append(
                f"{prefix}.check must be a YAML mapping with exactly one check-type key "
                f"(e.g. 'row_count:\\n  threshold:\\n    must_be_greater_than: 0'), got: {check_yaml!r}."
            )
            continue

        check_type = next(iter(parsed))
        if check_type not in _RECOGNIZED_CONTRACT_CHECK_TYPES:
            errors.append(
                f"{prefix}.check has type '{check_type}', which is not a recognized Soda Contract "
                f"Language check type (expected one of: {', '.join(_RECOGNIZED_CONTRACT_CHECK_TYPES)})."
            )

    return errors


def _invalid(errors: List[str]) -> ValidationResult:
    return ValidationResult(valid=False, error=ApiValidationError(errors=errors))
