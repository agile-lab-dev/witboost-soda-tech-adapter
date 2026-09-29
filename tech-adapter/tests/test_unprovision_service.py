from pathlib import Path
from unittest.mock import MagicMock

import yaml

from src.clients.soda_client import SodaApiError
from src.models.api_models import Status1
from src.models.data_product_descriptor import DataProduct
from src.models.task_store import TaskStore
from src.services.unprovision_service import run_unprovisioning

VALID_DESCRIPTOR_STR = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()
SODA_COMPONENT_ID = "urn:dmb:cmp:finance:orders:0:orders-output-port:soda-workload"
OUTPUT_PORT_ID = "urn:dmb:cmp:finance:orders:0:orders-output-port"


def _data_product_from(descriptor_str: str) -> DataProduct:
    raw = yaml.safe_load(descriptor_str)
    return DataProduct(**raw["dataProduct"])


def test_run_unprovisioning_success():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = MagicMock()
    client.find_contract_ids_for_dataset.return_value = ["contract-1"]
    client.clear_contract.return_value = None

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.COMPLETED
    result = state.info.publicInfo["sodaUnprovisioningResult"]
    assert result == [{"outputPortId": OUTPUT_PORT_ID, "sodaContractId": "contract-1"}]
    client.find_contract_ids_for_dataset.assert_called_once_with(
        "databricks-production/main/sales/orders"
    )
    client.clear_contract.assert_called_once_with("contract-1", "databricks-production/main/sales/orders")


def test_run_unprovisioning_prefers_output_port_specific_fields():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "        catalogName: main\n        schemaName: sales\n        tableName: orders\n",
        "        catalogName: main\n"
        "        schemaName: sales\n"
        "        tableName: orders\n"
        "        catalogNameOP: main\n"
        "        schemaNameOP: output-port\n"
        "        viewNameOP: orders_view\n",
    )
    data_product = _data_product_from(descriptor_str)
    store = TaskStore()
    token = store.create_running_task()
    client = MagicMock()
    client.find_contract_ids_for_dataset.return_value = ["contract-1"]
    client.clear_contract.return_value = None

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.COMPLETED
    client.find_contract_ids_for_dataset.assert_called_once_with(
        "databricks-production/main/output-port/orders_view"
    )
    client.clear_contract.assert_called_once_with(
        "contract-1", "databricks-production/main/output-port/orders_view"
    )


def test_run_unprovisioning_no_existing_contract_is_idempotent():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = MagicMock()
    client.find_contract_ids_for_dataset.return_value = []

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.COMPLETED
    assert state.info.publicInfo["sodaUnprovisioningResult"] == []
    client.clear_contract.assert_not_called()


def test_run_unprovisioning_component_not_found():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()

    run_unprovisioning(data_product, "urn:dmb:cmp:finance:orders:0:does-not-exist", token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "is not a Soda workload" in state.result


def test_run_unprovisioning_component_not_a_soda_workload():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()

    run_unprovisioning(data_product, OUTPUT_PORT_ID, token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "is not a Soda workload" in state.result


def test_run_unprovisioning_invalid_specific():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "              dataSourceName: databricks-production\n",
        "",
        1,
    )
    data_product = _data_product_from(descriptor_str)
    store = TaskStore()
    token = store.create_running_task()

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED


def test_run_unprovisioning_client_construction_failure(monkeypatch):
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()

    def raise_auth_error(*_args, **_kwargs):
        raise SodaApiError("AUTHENTICATION_FAILED", "no credentials configured")

    monkeypatch.setattr("src.services.unprovision_service.SodaClient", raise_auth_error)

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "AUTHENTICATION_FAILED" in state.result


def test_run_unprovisioning_output_port_not_found():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "                - urn:dmb:cmp:finance:orders:0:orders-output-port\n",
        "                - urn:dmb:cmp:finance:orders:0:missing-output-port\n",
        1,
    )
    data_product = _data_product_from(descriptor_str)
    store = TaskStore()
    token = store.create_running_task()
    client = MagicMock()

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "was not found in the Data Product descriptor" in state.result


def test_run_unprovisioning_soda_api_error():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = MagicMock()
    client.find_contract_ids_for_dataset.side_effect = SodaApiError("CONTRACT_API_ERROR", "lookup failed")

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "CONTRACT_API_ERROR" in state.result


def test_run_unprovisioning_unexpected_exception_is_captured():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = MagicMock()
    client.find_contract_ids_for_dataset.side_effect = RuntimeError("boom")

    run_unprovisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "Unexpected error during unprovisioning" in state.result
