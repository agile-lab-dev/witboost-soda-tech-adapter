from unittest.mock import MagicMock

import pytest
import yaml

from src.app_config import SodaConfig
from src.clients.soda_client import SodaApiError, SodaClient


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, content=b"{}"):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._json_data


def make_config(**overrides) -> SodaConfig:
    return SodaConfig(
        api_key_id=overrides.get("api_key_id", "id"),
        api_key_secret=overrides.get("api_key_secret", "secret"),
        base_url=overrides.get("base_url", "https://cloud.soda.io"),
        request_timeout_seconds=overrides.get("request_timeout_seconds", 30.0),
        polling_interval_seconds=overrides.get("polling_interval_seconds", 0.0),
        polling_max_timeout_seconds=overrides.get("polling_max_timeout_seconds", 0.05),
        polling_backoff_multiplier=overrides.get("polling_backoff_multiplier", 1.5),
        polling_backoff_max_interval_seconds=overrides.get("polling_backoff_max_interval_seconds", 1.0),
    )


def make_client(session, config=None) -> SodaClient:
    return SodaClient(config or make_config(), session=session, sleep_fn=lambda seconds: None)


def test_missing_credentials_raises_authentication_failed():
    config = make_config(api_key_id=None, api_key_secret=None)

    with pytest.raises(SodaApiError) as exc_info:
        SodaClient(config)

    assert exc_info.value.code == "AUTHENTICATION_FAILED"


def test_get_datasource_id_success():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"content": [{"id": "ds-1"}]})
    client = make_client(session)

    assert client.get_datasource_id("databricks-production") == "ds-1"


def test_get_datasource_id_not_found():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"content": []})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.get_datasource_id("databricks-production")

    assert exc_info.value.code == "DATA_SOURCE_NOT_FOUND"


def test_get_datasource_id_ambiguous():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"content": [{"id": "ds-1"}, {"id": "ds-2"}]})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.get_datasource_id("databricks-production")

    assert exc_info.value.code == "DATA_SOURCE_AMBIGUOUS"


def test_authentication_failed_on_401():
    session = MagicMock()
    session.request.return_value = FakeResponse(401, {})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.get_datasource_id("databricks-production")

    assert exc_info.value.code == "AUTHENTICATION_FAILED"


def test_trigger_discovery_success():
    session = MagicMock()
    session.request.return_value = FakeResponse(202, {}, headers={"X-Soda-Scan-Id": "scan-1"})
    client = make_client(session)

    assert client.trigger_discovery("ds-1") == "scan-1"


def test_trigger_discovery_missing_header():
    session = MagicMock()
    session.request.return_value = FakeResponse(202, {}, headers={})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.trigger_discovery("ds-1")

    assert exc_info.value.code == "UNEXPECTED_API_RESPONSE"


def test_wait_for_discovery_completes():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"state": "completed"})
    client = make_client(session)

    client.wait_for_discovery("scan-1")  # should not raise


def test_wait_for_discovery_completed_with_warnings_is_success():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"state": "completedWithWarnings"})
    client = make_client(session)

    client.wait_for_discovery("scan-1")  # should not raise


def test_wait_for_discovery_failed():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"state": "failed"})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.wait_for_discovery("scan-1")

    assert exc_info.value.code == "DISCOVERY_FAILED"


def test_wait_for_discovery_completed_with_errors_includes_cloud_url_detail():
    # Confirmed against a real Soda Cloud response: a failing scan status
    # payload has no free-text `message` field, only numeric error/failure/
    # warning counts (which can all be 0 even on `completedWithErrors`) and a
    # `cloudUrl` linking to the scan in the Soda Cloud UI. Surface both in the
    # raised error so operators get more than just the bare terminal state.
    session = MagicMock()
    session.request.return_value = FakeResponse(
        200,
        {
            "state": "completedWithErrors",
            "errors": 0,
            "failures": 0,
            "warnings": 0,
            "cloudUrl": "https://cloud.soda.io/o/org-1/scans/scan-1",
        },
    )
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.wait_for_discovery("scan-1")

    assert exc_info.value.code == "DISCOVERY_COMPLETED_WITH_ERRORS"
    assert "errors=0, failures=0, warnings=0" in exc_info.value.message
    assert "https://cloud.soda.io/o/org-1/scans/scan-1" in exc_info.value.message


def test_wait_for_discovery_timeout():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"state": "running"})
    client = make_client(session, config=make_config(polling_interval_seconds=0.01, polling_max_timeout_seconds=0.02))

    with pytest.raises(SodaApiError) as exc_info:
        client.wait_for_discovery("scan-1")

    assert exc_info.value.code == "DISCOVERY_POLLING_TIMEOUT"


def test_wait_for_discovery_retries_on_429():
    session = MagicMock()
    session.request.side_effect = [
        FakeResponse(429, {}, headers={"Retry-After": "0"}),
        FakeResponse(200, {"state": "completed"}),
    ]
    client = make_client(session)

    client.wait_for_discovery("scan-1")  # should not raise
    assert session.request.call_count == 2


def test_find_discovered_dataset_id_exact_match_required():
    session = MagicMock()
    session.request.return_value = FakeResponse(
        200,
        {
            "content": [
                {"id": "dd-1", "qualifiedName": "databricks-production/main/staging/orders"},
                {"id": "dd-2", "qualifiedName": "databricks-production/main/sales/orders"},
            ]
        },
    )
    client = make_client(session)

    dataset_id = client.find_discovered_dataset_id(
        "ds-1", "orders", "databricks-production/main/sales/orders"
    )

    assert dataset_id == "dd-2"


def test_find_discovered_dataset_id_not_found():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"content": []})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.find_discovered_dataset_id("ds-1", "orders", "databricks-production/main/sales/orders")

    assert exc_info.value.code == "DATASET_NOT_FOUND"


def test_trigger_onboarding_success():
    session = MagicMock()
    session.request.return_value = FakeResponse(
        202,
        {},
        headers={"Location": "https://cloud.soda.io/api/v1/datasources/ds-1/onboardDatasets/op-1"},
        content=b"",
    )
    client = make_client(session)

    operation_id, status_url = client.trigger_onboarding("ds-1", "dd-1")

    assert operation_id == "op-1"
    assert status_url == "https://cloud.soda.io/api/v1/datasources/ds-1/onboardDatasets/op-1"


def test_trigger_onboarding_uses_body_operation_id_when_present():
    session = MagicMock()
    session.request.return_value = FakeResponse(
        202,
        {"operationId": "op-1"},
        headers={"Location": "https://cloud.soda.io/api/v1/datasources/ds-1/onboardDatasets/op-1"},
    )
    client = make_client(session)

    operation_id, status_url = client.trigger_onboarding("ds-1", "dd-1")

    assert operation_id == "op-1"
    assert status_url == "https://cloud.soda.io/api/v1/datasources/ds-1/onboardDatasets/op-1"


def test_trigger_onboarding_missing_location_header_raises():
    # A 202 with no `Location` header and no body `operationId` leaves us
    # with no usable status-check URL; must fail fast instead of guessing.
    session = MagicMock()
    session.request.return_value = FakeResponse(202, {}, content=b"")
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.trigger_onboarding("ds-1", "dd-1")

    assert exc_info.value.code == "UNEXPECTED_API_RESPONSE"


def test_wait_for_onboarding_success_reuses_discovered_dataset_id():
    # Confirmed against a real Soda Cloud response: the onboarding operation
    # status payload has no `onboardedDatasetId`/`result` field at all; the
    # discovered dataset id is reused as the dataset id downstream.
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"id": "op-1", "state": "completed"})
    client = make_client(session)

    assert client.wait_for_onboarding("op-1", "https://cloud.soda.io/status", "dd-1") == "dd-1"


def test_wait_for_onboarding_completed_with_warnings_is_success():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"id": "op-1", "state": "completedWithWarnings"})
    client = make_client(session)

    assert client.wait_for_onboarding("op-1", "https://cloud.soda.io/status", "dd-1") == "dd-1"


def test_wait_for_onboarding_failure():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"id": "op-1", "state": "failed", "message": "boom"})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.wait_for_onboarding("op-1", "https://cloud.soda.io/status", "dd-1")

    assert exc_info.value.code == "ONBOARDING_FAILED"


def test_wait_for_onboarding_timeout():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"id": "op-1", "state": "running"})
    client = make_client(session, config=make_config(polling_interval_seconds=0.01, polling_max_timeout_seconds=0.02))

    with pytest.raises(SodaApiError) as exc_info:
        client.wait_for_onboarding("op-1", "https://cloud.soda.io/status", "dd-1")

    assert exc_info.value.code == "ONBOARDING_POLLING_TIMEOUT"


def test_wait_for_onboarding_empty_body_fails_fast():
    # A 200 with a completely empty body indicates the endpoint/response
    # shape assumption is wrong; must fail immediately instead of polling
    # uselessly until polling_max_timeout_seconds.
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {}, content=b"")
    client = make_client(session, config=make_config(polling_max_timeout_seconds=100.0))

    with pytest.raises(SodaApiError) as exc_info:
        client.wait_for_onboarding("op-1", "https://cloud.soda.io/status", "dd-1")

    assert exc_info.value.code == "UNEXPECTED_API_RESPONSE"
    assert session.request.call_count == 1


def test_create_contract_success():
    session = MagicMock()
    session.request.side_effect = [
        FakeResponse(200, {"content": []}),  # find_contract_ids_for_dataset lookup: no existing contract
        FakeResponse(201, {"contract": {"id": "contract-1"}}),  # POST /api/v1/contracts
    ]
    client = make_client(session)

    assert client.create_contract("od-1", "databricks-production/main/sales/orders") == "contract-1"
    assert session.request.call_count == 2


def test_create_contract_reuses_existing_contract():
    # Idempotency: if a contract already exists for the dataset, create_contract
    # must reuse it instead of calling POST /api/v1/contracts (which would fail
    # with HTTP 400 `dataset_contract_already_exists` on a real Soda Cloud tenant).
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"content": [{"id": "existing-contract"}]})
    client = make_client(session)

    assert client.create_contract("od-1", "databricks-production/main/sales/orders") == "existing-contract"
    assert session.request.call_count == 1


def test_create_contract_concurrent_creation_race_falls_back_to_lookup():
    # If POST /api/v1/contracts races with a concurrent provisioning run and
    # returns HTTP 400 `dataset_contract_already_exists`, create_contract must
    # look the contract up again instead of failing.
    session = MagicMock()
    session.request.side_effect = [
        FakeResponse(200, {"content": []}),  # initial lookup: nothing found
        FakeResponse(400, {"code": "dataset_contract_already_exists"}),  # POST races and loses
        FakeResponse(200, {"content": [{"id": "contract-created-concurrently"}]}),  # re-lookup finds it
    ]
    client = make_client(session)

    assert client.create_contract("od-1", "databricks-production/main/sales/orders") == "contract-created-concurrently"
    assert session.request.call_count == 3


def test_create_contract_missing_id_raises():
    session = MagicMock()
    session.request.side_effect = [
        FakeResponse(200, {"content": []}),  # find_contract_ids_for_dataset lookup: no existing contract
        FakeResponse(201, {"contract": {}}),  # POST /api/v1/contracts
    ]
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.create_contract("od-1", "databricks-production/main/sales/orders")

    assert exc_info.value.code == "UNEXPECTED_API_RESPONSE"


def test_publish_contract_success():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"contract": {"id": "contract-1"}})
    client = make_client(session)

    client.publish_contract("contract-1", "dataset: ds\nchecks:\n  - row_count:\ncolumns: []\n")  # should not raise


def test_publish_contract_failure():
    session = MagicMock()
    session.request.return_value = FakeResponse(400, {"code": "invalid_request"})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.publish_contract("contract-1", "dataset: ds\nchecks: []\ncolumns: []\n")

    assert exc_info.value.code == "CONTRACT_PUBLISH_FAILED"


def test_find_contract_ids_for_dataset():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"content": [{"id": "contract-1"}, {"id": "contract-2"}]})
    client = make_client(session)

    assert client.find_contract_ids_for_dataset("fqn") == ["contract-1", "contract-2"]


def test_find_contract_ids_for_dataset_404_returns_empty():
    session = MagicMock()
    session.request.return_value = FakeResponse(404, {})
    client = make_client(session)

    assert client.find_contract_ids_for_dataset("fqn") == []


def test_clear_contract_publishes_empty_contents():
    session = MagicMock()
    session.request.return_value = FakeResponse(200, {"contract": {"id": "contract-1"}})
    client = make_client(session)

    client.clear_contract("contract-1", "ds/cat/schema/table")

    session.request.assert_called_once()
    args, kwargs = session.request.call_args
    method, url = args[0], args[1]
    assert method == "POST"
    assert url.endswith("/api/v1/contracts/contract-1")
    sent_contents = kwargs["json"]["contents"]
    assert yaml.safe_load(sent_contents) == {"dataset": "ds/cat/schema/table", "columns": []}


def test_clear_contract_failure():
    session = MagicMock()
    session.request.return_value = FakeResponse(500, {})
    client = make_client(session)

    with pytest.raises(SodaApiError) as exc_info:
        client.clear_contract("contract-1", "ds/cat/schema/table")

    assert exc_info.value.code == "CONTRACT_PUBLISH_FAILED"
