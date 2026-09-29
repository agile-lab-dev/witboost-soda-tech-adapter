from __future__ import annotations

from typing import Dict, List, Optional

import yaml
from loguru import logger

from src.app_config import soda_config
from src.clients.soda_client import SodaApiError, SodaClient
from src.models.api_models import Info
from src.models.constants import SODA_TECH_ADAPTER_INFRASTRUCTURE_TEMPLATE_ID
from src.models.data_product_descriptor import DataProduct, OutputPort, QualityCheck, Workload
from src.models.soda_specific import extract_soda_platform_specific
from src.models.task_store import TaskStore
from src.models.task_store import task_store as default_task_store
from src.services.output_port_metadata import resolve_output_port_table_metadata


def run_provisioning(
    data_product: DataProduct,
    component_id: str,
    token: str,
    store: TaskStore = default_task_store,
    soda_client: Optional[SodaClient] = None,
) -> None:
    """
    Executes the full provisioning workflow (phases 0-6, see
    docs/technical-details.md) for a Soda workload component and stores the
    outcome in `store` under `token`.

    Designed to run inside a FastAPI BackgroundTask, scheduled after
    `POST /v1/provision` has already returned `202` + `token`. Never raises:
    all failures (including unexpected exceptions) are captured and recorded
    as a FAILED task, so the caller polling `GET /v1/provision/{token}/status`
    always converges to a terminal state.
    """
    logger.info("[{}] Starting provisioning for component '{}'.", token, component_id)

    def fail(message: str) -> None:
        logger.error("[{}] Provisioning failed: {}", token, message)
        store.set_failed(token, message)

    try:
        component = data_product.get_component_by_id(component_id)
        if not isinstance(component, Workload) or (
            component.infrastructureTemplateId != SODA_TECH_ADAPTER_INFRASTRUCTURE_TEMPLATE_ID
        ):
            fail(f"Component '{component_id}' is not a Soda workload.")
            return

        extraction = extract_soda_platform_specific(component.specific)
        if isinstance(extraction, list):
            fail("; ".join(extraction))
            return
        _platform_key, soda_specific = extraction

        try:
            client = soda_client or SodaClient(soda_config)
        except SodaApiError as ex:
            fail(f"{ex.code}: {ex.message}")
            return

        results = []
        for output_port_id in soda_specific.outputPortIds:
            output_port = data_product.get_component_by_id(output_port_id)
            if not isinstance(output_port, OutputPort):
                fail(
                    f"Output Port '{output_port_id}' referenced by the Soda workload was not "
                    f"found in the Data Product descriptor."
                )
                return

            logger.info("[{}] Provisioning Output Port '{}'...", token, output_port_id)
            try:
                result = _provision_output_port(client, soda_specific.dataSourceName, output_port)
            except SodaApiError as ex:
                fail(f"Provisioning failed for Output Port '{output_port_id}' ({ex.code}): {ex.message}")
                return

            logger.info("[{}] Output Port '{}' provisioned successfully.", token, output_port_id)
            results.append(result)

        store.set_completed(
            token,
            result="",
            info=Info(publicInfo={"sodaProvisioningResult": results}, privateInfo={}),
        )
        logger.info("[{}] Provisioning completed for component '{}'.", token, component_id)
    except Exception as ex:  # defensive: never leave a task stuck in RUNNING
        logger.exception("[{}] Unexpected error during Soda provisioning", token)
        store.set_failed(token, f"Unexpected error during provisioning: {ex}")


def _provision_output_port(client: SodaClient, data_source_name: str, output_port: OutputPort) -> dict:
    catalog, schema, table = resolve_output_port_table_metadata(output_port)
    if not table:
        raise SodaApiError(
            "OUTPUT_PORT_NOT_FOUND",
            f"Output Port '{output_port.id}' is missing 'specific.viewNameOP' (or 'specific.tableName').",
        )
    fully_qualified_name = f"{data_source_name}/{catalog}/{schema}/{table}"
    contents = _build_contract_contents(fully_qualified_name, output_port.dataContract.quality or [])

    datasource_id = client.get_datasource_id(data_source_name)

    scan_id = client.trigger_discovery(datasource_id)
    client.wait_for_discovery(scan_id)

    discovered_dataset_id = client.find_discovered_dataset_id(datasource_id, table, fully_qualified_name)

    operation_id, status_url = client.trigger_onboarding(datasource_id, discovered_dataset_id)
    onboarded_dataset_id = client.wait_for_onboarding(operation_id, status_url, discovered_dataset_id)

    contract_id = client.create_contract(onboarded_dataset_id, fully_qualified_name)
    client.publish_contract(contract_id, contents)

    return {
        "outputPortId": output_port.id,
        "sodaContractId": contract_id,
        "sodaDatasetId": onboarded_dataset_id,
        "dataSourceName": data_source_name,
    }


def _build_contract_contents(fully_qualified_name: str, quality_checks: List[QualityCheck]) -> str:
    """
    Builds a Soda Contract Language YAML document (see
    https://docs.soda.io/reference/contract-language-reference) from the
    Data Product descriptor's `dataContract.quality` checks.

    Each `QualityCheck.check` is a raw YAML fragment for a single check-type
    mapping (e.g. `"row_count:\\n  threshold:\\n    must_be_greater_than: 0"`).
    Checks without a `column` are placed under the dataset-level `checks:`
    list; checks with a `column` are grouped under that column's `checks:`
    list in the `columns:` list.
    """
    dataset_level_checks: List[dict] = []
    column_level_checks: Dict[str, List[dict]] = {}

    for quality_check in quality_checks:
        parsed_check = yaml.safe_load(quality_check.check)
        if quality_check.column:
            column_level_checks.setdefault(quality_check.column, []).append(parsed_check)
        else:
            dataset_level_checks.append(parsed_check)

    contract: Dict[str, object] = {"dataset": fully_qualified_name}
    if dataset_level_checks:
        contract["checks"] = dataset_level_checks
    contract["columns"] = [
        {"name": column_name, "checks": checks} for column_name, checks in column_level_checks.items()
    ]

    return yaml.safe_dump(contract, sort_keys=False)
