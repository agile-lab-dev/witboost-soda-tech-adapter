from __future__ import annotations

from typing import List, Optional

from loguru import logger

from src.app_config import soda_config
from src.clients.soda_client import SodaApiError, SodaClient
from src.models.api_models import Info
from src.models.constants import SODA_TECH_ADAPTER_INFRASTRUCTURE_TEMPLATE_ID
from src.models.data_product_descriptor import DataProduct, OutputPort, Workload
from src.models.soda_specific import extract_soda_platform_specific
from src.models.task_store import TaskStore
from src.models.task_store import task_store as default_task_store
from src.services.output_port_metadata import resolve_output_port_table_metadata


def run_unprovisioning(
    data_product: DataProduct,
    component_id: str,
    token: str,
    store: TaskStore = default_task_store,
    soda_client: Optional[SodaClient] = None,
) -> None:
    """
    Clears the data quality checks from the Soda Data Contract(s) for each
    Output Port declared by a Soda workload component (see
    docs/technical-details.md, Unprovisioning), by republishing each
    contract with an empty skeleton (no checks). The contract resource and
    the underlying Soda Data Source remain in place — the Soda Data Source
    is pre-provisioned shared infrastructure and is never removed. The
    operation is idempotent: an Output Port with no existing contract is
    treated as already unprovisioned, not a failure.

    Designed to run inside a FastAPI BackgroundTask, scheduled after
    `POST /v1/unprovision` has already returned `202` + `token`. Never
    raises: all failures are captured and recorded as a FAILED task.
    """
    logger.info("[{}] Starting unprovisioning for component '{}'.", token, component_id)

    def fail(message: str) -> None:
        logger.error("[{}] Unprovisioning failed: {}", token, message)
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

        cleared_contracts: List[dict] = []
        for output_port_id in soda_specific.outputPortIds:
            output_port = data_product.get_component_by_id(output_port_id)
            if not isinstance(output_port, OutputPort):
                fail(
                    f"Output Port '{output_port_id}' referenced by the Soda workload was not "
                    f"found in the Data Product descriptor."
                )
                return

            logger.info("[{}] Unprovisioning Output Port '{}'...", token, output_port_id)
            try:
                cleared_contracts.extend(_unprovision_output_port(client, soda_specific.dataSourceName, output_port))
            except SodaApiError as ex:
                fail(f"Unprovisioning failed for Output Port '{output_port_id}' ({ex.code}): {ex.message}")
                return

            logger.info("[{}] Output Port '{}' unprovisioned successfully.", token, output_port_id)

        store.set_completed(
            token,
            result="",
            info=Info(publicInfo={"sodaUnprovisioningResult": cleared_contracts}, privateInfo={}),
        )
        logger.info("[{}] Unprovisioning completed for component '{}'.", token, component_id)
    except Exception as ex:
        logger.exception("[{}] Unexpected error during Soda unprovisioning", token)
        store.set_failed(token, f"Unexpected error during unprovisioning: {ex}")


def _unprovision_output_port(client: SodaClient, data_source_name: str, output_port: OutputPort) -> List[dict]:
    catalog, schema, table = resolve_output_port_table_metadata(output_port)
    fully_qualified_name = f"{data_source_name}/{catalog}/{schema}/{table}"

    contract_ids = client.find_contract_ids_for_dataset(fully_qualified_name)

    cleared = []
    for contract_id in contract_ids:
        client.clear_contract(contract_id, fully_qualified_name)
        cleared.append({"outputPortId": output_port.id, "sodaContractId": contract_id})

    return cleared
