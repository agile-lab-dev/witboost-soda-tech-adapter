from pathlib import Path
from unittest.mock import MagicMock

import yaml

from src.clients.soda_client import SodaApiError
from src.models.api_models import Status1
from src.models.data_product_descriptor import DataProduct
from src.models.task_store import TaskStore
from src.services.provision_service import run_provisioning

VALID_DESCRIPTOR_STR = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()
SODA_COMPONENT_ID = "urn:dmb:cmp:finance:orders:0:orders-output-port:soda-workload"
OUTPUT_PORT_ID = "urn:dmb:cmp:finance:orders:0:orders-output-port"


def _data_product_from(descriptor_str: str) -> DataProduct:
    raw = yaml.safe_load(descriptor_str)
    return DataProduct(**raw["dataProduct"])


def _fake_client() -> MagicMock:
    client = MagicMock()
    client.get_datasource_id.return_value = "datasource-1"
    client.trigger_discovery.return_value = "scan-1"
    client.wait_for_discovery.return_value = None
    client.find_discovered_dataset_id.return_value = "discovered-1"
    client.trigger_onboarding.return_value = ("operation-1", "https://cloud.soda.io/status")
    client.wait_for_onboarding.return_value = "dataset-1"
    client.create_contract.return_value = "contract-1"
    client.publish_contract.return_value = None
    return client


def test_run_provisioning_success():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = _fake_client()

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.COMPLETED
    result = state.info.publicInfo["sodaProvisioningResult"]
    assert result == [
        {
            "outputPortId": OUTPUT_PORT_ID,
            "sodaContractId": "contract-1",
            "sodaDatasetId": "dataset-1",
            "dataSourceName": "databricks-production",
        }
    ]
    client.get_datasource_id.assert_called_once_with("databricks-production")
    client.trigger_discovery.assert_called_once_with("datasource-1")
    client.find_discovered_dataset_id.assert_called_once_with(
        "datasource-1", "orders", "databricks-production/main/sales/orders"
    )
    client.create_contract.assert_called_once_with("dataset-1", "databricks-production/main/sales/orders")
    client.publish_contract.assert_called_once()


def test_run_provisioning_prefers_output_port_specific_fields():
    """
    When an Output Port exposes both the underlying storage table
    (catalogName/schemaName/tableName) and its actual exposed asset
    (catalogNameOP/schemaNameOP/viewNameOP), the tech adapter must target
    Soda discovery/onboarding/contract at the exposed OP asset, not the
    internal storage table.
    """
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
    client = _fake_client()

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.COMPLETED
    client.find_discovered_dataset_id.assert_called_once_with(
        "datasource-1", "orders_view", "databricks-production/main/output-port/orders_view"
    )
    client.create_contract.assert_called_once_with(
        "dataset-1", "databricks-production/main/output-port/orders_view"
    )


def test_run_provisioning_component_not_found():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()

    run_provisioning(data_product, "urn:dmb:cmp:finance:orders:0:does-not-exist", token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "is not a Soda workload" in state.result


def test_run_provisioning_component_not_a_soda_workload():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()

    run_provisioning(data_product, OUTPUT_PORT_ID, token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "is not a Soda workload" in state.result


def test_run_provisioning_invalid_specific():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "              dataSourceName: databricks-production\n",
        "",
        1,
    )
    data_product = _data_product_from(descriptor_str)
    store = TaskStore()
    token = store.create_running_task()

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED


def test_run_provisioning_client_construction_failure(monkeypatch):
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()

    def raise_auth_error(*_args, **_kwargs):
        raise SodaApiError("AUTHENTICATION_FAILED", "no credentials configured")

    monkeypatch.setattr("src.services.provision_service.SodaClient", raise_auth_error)

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "AUTHENTICATION_FAILED" in state.result


def test_run_provisioning_output_port_not_found():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "                - urn:dmb:cmp:finance:orders:0:orders-output-port\n",
        "                - urn:dmb:cmp:finance:orders:0:missing-output-port\n",
        1,
    )
    data_product = _data_product_from(descriptor_str)
    store = TaskStore()
    token = store.create_running_task()
    client = _fake_client()

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "was not found in the Data Product descriptor" in state.result


def test_run_provisioning_soda_api_error_during_output_port_provisioning():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = _fake_client()
    client.get_datasource_id.side_effect = SodaApiError("DATA_SOURCE_NOT_FOUND", "not found")

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "DATA_SOURCE_NOT_FOUND" in state.result


def test_run_provisioning_missing_table_metadata():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "        tableName: orders\n",
        "",
        1,
    )
    data_product = _data_product_from(descriptor_str)
    store = TaskStore()
    token = store.create_running_task()
    client = _fake_client()

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "OUTPUT_PORT_NOT_FOUND" in state.result


def test_run_provisioning_unexpected_exception_is_captured():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)
    store = TaskStore()
    token = store.create_running_task()
    client = _fake_client()
    client.get_datasource_id.side_effect = RuntimeError("boom")

    run_provisioning(data_product, SODA_COMPONENT_ID, token, store=store, soda_client=client)

    state = store.get(token)
    assert state.status == Status1.FAILED
    assert "Unexpected error during provisioning" in state.result
