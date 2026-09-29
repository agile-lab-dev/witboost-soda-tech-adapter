from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

import requests
import yaml
from loguru import logger

from src.app_config import SodaConfig

# Minimum floor for a single polling wait step. Guards against a
# misconfigured (or test-provided) `polling_interval_seconds` of `0`, which
# would otherwise leave `elapsed` stuck at `0` forever and turn the polling
# loop into a true infinite loop instead of eventually raising a timeout.
_MIN_POLL_INTERVAL_SECONDS = 0.01


class SodaApiError(Exception):
    """
    Raised when a Soda Cloud API interaction fails in a way that should stop
    the current provisioning/unprovisioning phase. `code` matches one of the
    failure codes documented in docs/technical-details.md (Failure Matrix).
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _extract_items(payload: Any) -> List[dict]:
    """
    Normalize a Soda Cloud API list response into a plain list of dict items.
    Accepts either a bare JSON array or a paginated `{"content": [...]}` shape.
    The paginated `{"content": [...]}` shape is what Soda Cloud's Data
    Source, Discovered Dataset, and Contract list endpoints return in
    practice.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, list):
            return content
    return []


def _safe_json(response: requests.Response) -> Any:
    """
    Parse a response body as JSON, tolerating empty bodies (e.g. a `202
    Accepted` with no content, as returned by some Soda Cloud async
    endpoints). Returns `{}` instead of raising when the body is empty or
    not valid JSON.
    """
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def _retry_after_seconds(response: requests.Response, default: float) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return default


class SodaClient:
    """
    Thin REST client for the Soda Cloud API used by the provisioning and
    unprovisioning services. Authenticates via HTTP Basic auth using
    `SODA_API_KEY_ID`/`SODA_API_KEY_SECRET` (see docs/technical-details.md).
    """

    def __init__(
        self,
        config: SodaConfig,
        session: Optional[requests.Session] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if not config.is_configured():
            raise SodaApiError(
                "AUTHENTICATION_FAILED",
                "Soda Cloud API credentials are not configured (SODA_API_KEY_ID/SODA_API_KEY_SECRET).",
            )
        self._config = config
        self._session = session or requests.Session()
        # is_configured() above already guarantees both values are non-None.
        assert config.api_key_id is not None and config.api_key_secret is not None
        self._session.auth = (config.api_key_id, config.api_key_secret)
        self._sleep = sleep_fn

    def _request(
        self, method: str, path: str, *, error_code: str, error_context: str, **kwargs: Any
    ) -> requests.Response:
        url = path if path.startswith("http://") or path.startswith("https://") else f"{self._config.base_url}{path}"
        try:
            response = self._session.request(method, url, timeout=self._config.request_timeout_seconds, **kwargs)
        except requests.RequestException as ex:
            raise SodaApiError(error_code, f"{error_context}: request failed ({ex}).") from ex

        if response.status_code in (401, 403):
            raise SodaApiError(
                "AUTHENTICATION_FAILED",
                f"{error_context}: Soda Cloud API returned {response.status_code} (check API key permissions).",
            )

        return response

    # ---- Phase 1: Data Source Resolution ----

    def get_datasource_id(self, data_source_name: str) -> str:
        logger.info("Looking up Soda Data Source '{}'...", data_source_name)
        response = self._request(
            "GET",
            "/api/v1/datasources",
            params={"search": data_source_name},
            error_code="DATA_SOURCE_API_ERROR",
            error_context=f"Data Source lookup for '{data_source_name}'",
        )
        if response.status_code != 200:
            raise SodaApiError(
                "DATA_SOURCE_API_ERROR",
                f"Data Source lookup for '{data_source_name}' failed with HTTP {response.status_code}.",
            )

        results = _extract_items(_safe_json(response))

        if len(results) == 0:
            raise SodaApiError(
                "DATA_SOURCE_NOT_FOUND",
                f"No Soda Data Source found matching name '{data_source_name}'.",
            )
        if len(results) > 1:
            raise SodaApiError(
                "DATA_SOURCE_AMBIGUOUS",
                f"Multiple Soda Data Sources ({len(results)}) match name '{data_source_name}'; expected exactly 1.",
            )

        datasource_id = results[0].get("id")
        if not datasource_id:
            raise SodaApiError(
                "UNEXPECTED_API_RESPONSE",
                f"Data Source lookup for '{data_source_name}' returned a result without an 'id' field.",
            )
        logger.info("Resolved Soda Data Source '{}' -> id '{}'.", data_source_name, datasource_id)
        return datasource_id

    # ---- Phase 2: Discovery ----

    def trigger_discovery(self, datasource_id: str) -> str:
        logger.info("Triggering discovery scan on datasource '{}'...", datasource_id)
        response = self._request(
            "POST",
            f"/api/v1/datasources/{datasource_id}/discover",
            error_code="DISCOVERY_TRIGGER_FAILED",
            error_context=f"Discovery trigger for datasource '{datasource_id}'",
        )
        if response.status_code not in (200, 201, 202):
            raise SodaApiError(
                "DISCOVERY_TRIGGER_FAILED",
                f"Discovery trigger for datasource '{datasource_id}' failed with HTTP {response.status_code}.",
            )

        scan_id = response.headers.get("X-Soda-Scan-Id")
        if not scan_id:
            raise SodaApiError(
                "UNEXPECTED_API_RESPONSE",
                f"Discovery trigger for datasource '{datasource_id}' did not return an X-Soda-Scan-Id header.",
            )
        logger.info("Discovery scan triggered on datasource '{}' -> scan id '{}'.", datasource_id, scan_id)
        return scan_id

    def wait_for_discovery(self, scan_id: str) -> None:
        logger.info("Waiting for discovery scan '{}' to complete...", scan_id)
        terminal_success = {"completed", "completedWithWarnings"}
        terminal_failure = {
            "completedWithErrors": "DISCOVERY_COMPLETED_WITH_ERRORS",
            "completedWithFailures": "DISCOVERY_COMPLETED_WITH_FAILURES",
            "failed": "DISCOVERY_FAILED",
            "timedOut": "DISCOVERY_TIMED_OUT",
            "canceled": "DISCOVERY_CANCELED",
        }

        interval = self._config.polling_interval_seconds
        elapsed = 0.0
        while elapsed < self._config.polling_max_timeout_seconds:
            response = self._request(
                "GET",
                f"/api/v1/scans/{scan_id}",
                error_code="DISCOVERY_TRIGGER_FAILED",
                error_context=f"Scan status lookup for scan '{scan_id}'",
            )

            if response.status_code == 429:
                wait = _retry_after_seconds(response, interval * self._config.polling_backoff_multiplier)
                self._sleep(wait)
                elapsed += wait
                continue

            if response.status_code != 200:
                raise SodaApiError(
                    "DISCOVERY_TRIGGER_FAILED",
                    f"Scan status lookup for '{scan_id}' failed with HTTP {response.status_code}.",
                )

            payload = _safe_json(response)
            status = payload.get("state")
            if status in terminal_success:
                logger.info("Discovery scan '{}' completed with state '{}'.", scan_id, status)
                return
            if status in terminal_failure:
                # Confirmed against a real Soda Cloud response: a scan status
                # payload has no free-text `message`/`logs` field, only numeric
                # `errors`/`failures`/`warnings` counts (which can legitimately
                # all be 0 even on a `completedWithErrors` state, e.g. when the
                # failure is a datasource/agent-level connectivity or
                # permission issue rather than a per-check/per-dataset error).
                # `cloudUrl` links directly to the scan in the Soda Cloud UI,
                # which is the only place the underlying error detail is
                # currently visible.
                logger.error(
                    "Discovery scan '{}' ended with state '{}'. Full status payload: {}",
                    scan_id,
                    status,
                    payload,
                )
                counts = (
                    f"errors={payload.get('errors')}, failures={payload.get('failures')}, "
                    f"warnings={payload.get('warnings')}"
                )
                cloud_url = payload.get("cloudUrl")
                detail_suffix = f" ({counts})"
                if cloud_url:
                    detail_suffix += f" See scan details: {cloud_url}"
                raise SodaApiError(
                    terminal_failure[status],
                    f"Discovery scan '{scan_id}' ended with state '{status}'.{detail_suffix}",
                )

            logger.debug("Discovery scan '{}' still in state '{}', polling again in {}s.", scan_id, status, interval)
            self._sleep(interval)
            elapsed += max(interval, _MIN_POLL_INTERVAL_SECONDS)
            interval = min(
                interval * self._config.polling_backoff_multiplier,
                self._config.polling_backoff_max_interval_seconds,
            )

        raise SodaApiError(
            "DISCOVERY_POLLING_TIMEOUT",
            f"Discovery scan '{scan_id}' did not complete within {self._config.polling_max_timeout_seconds}s.",
        )

    # ---- Phase 3: Dataset Identification ----

    def find_discovered_dataset_id(self, datasource_id: str, table_name: str, fully_qualified_name: str) -> str:
        logger.info("Looking up discovered dataset '{}'...", fully_qualified_name)
        response = self._request(
            "GET",
            "/api/v1/discoveredDatasets",
            params={"datasourceId": datasource_id, "search": table_name},
            error_code="DATASET_API_ERROR",
            error_context=f"Discovered dataset lookup for '{fully_qualified_name}'",
        )
        if response.status_code != 200:
            raise SodaApiError(
                "DATASET_API_ERROR",
                f"Discovered dataset lookup for '{fully_qualified_name}' failed with HTTP {response.status_code}.",
            )

        items = _extract_items(_safe_json(response))
        matches = [item for item in items if item.get("qualifiedName") == fully_qualified_name]

        if len(matches) == 0:
            raise SodaApiError(
                "DATASET_NOT_FOUND",
                f"No discovered dataset found matching fully qualified name '{fully_qualified_name}'.",
            )
        if len(matches) > 1:
            raise SodaApiError(
                "DATASET_AMBIGUOUS",
                f"Multiple discovered datasets ({len(matches)}) match fully qualified name '{fully_qualified_name}'.",
            )

        dataset_id = matches[0].get("id")
        if not dataset_id:
            raise SodaApiError(
                "UNEXPECTED_API_RESPONSE",
                f"Discovered dataset match for '{fully_qualified_name}' has no 'id' field.",
            )
        logger.info("Resolved discovered dataset '{}' -> id '{}'.", fully_qualified_name, dataset_id)
        return dataset_id

    # ---- Phase 4: Dataset Onboarding ----

    def trigger_onboarding(self, datasource_id: str, discovered_dataset_id: str) -> tuple[str, str]:
        logger.info("Triggering onboarding for discovered dataset '{}'...", discovered_dataset_id)
        response = self._request(
            "POST",
            f"/api/v1/datasources/{datasource_id}/onboardDatasets",
            json={"discoveredDatasetIds": [discovered_dataset_id]},
            error_code="ONBOARDING_TRIGGER_FAILED",
            error_context=f"Onboarding trigger for dataset '{discovered_dataset_id}'",
        )

        logger.debug(
            "Onboarding trigger response: status={} headers={} body={}",
            response.status_code,
            dict(response.headers),
            _safe_json(response),
        )

        if response.status_code not in (200, 201, 202):
            raise SodaApiError(
                "ONBOARDING_TRIGGER_FAILED",
                f"Onboarding trigger for dataset '{discovered_dataset_id}' failed with HTTP {response.status_code}.",
            )

        # Confirmed against a real Soda Cloud response: a `202 Accepted` with
        # an empty body and the operation status URL in the standard `Location`
        # header, e.g.
        # https://cloud.soda.io/api/v1/datasources/{datasourceId}/onboardDatasets/{operationId}
        # The response body `operationId` field is kept as a fallback in case
        # a future/alternate response shape includes it directly.
        status_url = response.headers.get("Location")
        operation_id = _safe_json(response).get("operationId") or (
            status_url.rsplit("/", 1)[-1] if status_url else None
        )
        if not operation_id or not status_url:
            raise SodaApiError(
                "UNEXPECTED_API_RESPONSE",
                f"Onboarding trigger for dataset '{discovered_dataset_id}' did not return a usable operation "
                f"reference (checked body 'operationId' and 'Location' header).",
            )
        logger.info("Onboarding triggered for dataset '{}' -> operation id '{}'.", discovered_dataset_id, operation_id)
        return operation_id, status_url

    def wait_for_onboarding(self, operation_id: str, status_url: str, discovered_dataset_id: str) -> str:
        logger.info("Waiting for onboarding operation '{}' to complete...", operation_id)
        # Confirmed against a real Soda Cloud response: the terminal state
        # value is `completed` (not `success`), mirroring the discovery scan
        # state vocabulary. Failure state names are not yet confirmed against
        # a real failing onboarding operation; they are assumed to mirror the
        # discovery scan failure vocabulary until proven otherwise.
        terminal_success = {"completed", "completedWithWarnings"}
        terminal_failure = {
            "completedWithErrors": "ONBOARDING_COMPLETED_WITH_ERRORS",
            "completedWithFailures": "ONBOARDING_COMPLETED_WITH_FAILURES",
            "failed": "ONBOARDING_FAILED",
            "timedOut": "ONBOARDING_TIMED_OUT",
            "canceled": "ONBOARDING_CANCELED",
        }
        interval = self._config.polling_interval_seconds
        elapsed = 0.0
        while elapsed < self._config.polling_max_timeout_seconds:
            response = self._request(
                "GET",
                status_url,
                error_code="ONBOARDING_API_ERROR",
                error_context=f"Onboarding operation status lookup for '{operation_id}'",
            )

            if response.status_code == 429:
                wait = _retry_after_seconds(response, interval * self._config.polling_backoff_multiplier)
                self._sleep(wait)
                elapsed += wait
                continue

            if response.status_code != 200:
                raise SodaApiError(
                    "ONBOARDING_API_ERROR",
                    f"Onboarding operation status lookup for '{operation_id}' failed with HTTP {response.status_code}.",
                )

            payload = _safe_json(response)
            logger.debug("Onboarding operation '{}' status response payload: {}", operation_id, payload)

            if not payload:
                # An empty/unparseable body on a 200 response means the operation
                # status payload does not match the expected shape (a JSON object
                # with `state`). Fail fast with full diagnostics instead of
                # polling uselessly for polling_max_timeout_seconds.
                raise SodaApiError(
                    "UNEXPECTED_API_RESPONSE",
                    f"Onboarding operation status lookup for '{operation_id}' ('{status_url}') returned HTTP 200 "
                    f"with an empty/unparseable body (raw content: {response.content!r}, "
                    f"content-type: {response.headers.get('Content-Type')!r}). The response shape assumption is "
                    f"likely incorrect for this Soda Cloud tenant/API version.",
                )

            state = payload.get("state")

            if state in terminal_success:
                # Confirmed against a real Soda Cloud response: the operation
                # status payload does not include an `onboardedDatasetId` (or
                # any `result` object) at all -- only `id`/`state`/`started`/
                # `ended`/`message`. Soda Cloud does not appear to mint a new
                # dataset id during onboarding; the discovered dataset id is
                # reused as the dataset id for subsequent contract calls.
                logger.info(
                    "Onboarding operation '{}' completed with state '{}' -> using discovered dataset id '{}' "
                    "as the onboarded dataset id.",
                    operation_id,
                    state,
                    discovered_dataset_id,
                )
                return discovered_dataset_id
            if state in terminal_failure:
                raise SodaApiError(
                    terminal_failure[state],
                    f"Onboarding operation '{operation_id}' ended with state '{state}': "
                    f"{payload.get('message', 'no details')}.",
                )

            logger.debug(
                "Onboarding operation '{}' still in state '{}', polling again in {}s.", operation_id, state, interval
            )
            self._sleep(interval)
            elapsed += max(interval, _MIN_POLL_INTERVAL_SECONDS)
            interval = min(
                interval * self._config.polling_backoff_multiplier,
                self._config.polling_backoff_max_interval_seconds,
            )

        raise SodaApiError(
            "ONBOARDING_POLLING_TIMEOUT",
            f"Onboarding operation '{operation_id}' did not complete within "
            f"{self._config.polling_max_timeout_seconds}s.",
        )

    # ---- Phase 5: Contract Creation ----

    def create_contract(self, dataset_id: str, fully_qualified_name: str) -> str:
        # `POST /api/v1/contracts` requires a `contents` field (Contract
        # Language YAML) even when creating the contract; a minimal skeleton
        # (dataset id + empty columns list) is sent here, and the real check
        # content is attached afterwards via `publish_contract`. The response
        # wraps the created contract under a `contract` key
        # (`{"contract": {"id": ..., ...}}`).
        #
        # Soda Cloud allows at most one contract per dataset: a second
        # `POST /api/v1/contracts` for the same dataset is rejected with
        # HTTP 400 `dataset_contract_already_exists`. To keep re-provisioning
        # an already-provisioned Output Port working, an existing contract
        # for the dataset is looked up first and reused if found.
        existing_contract_ids = self.find_contract_ids_for_dataset(fully_qualified_name)
        if existing_contract_ids:
            contract_id = existing_contract_ids[0]
            logger.info(
                "Soda contract '{}' already exists for dataset '{}', reusing it.", contract_id, dataset_id
            )
            return contract_id

        contents = yaml.safe_dump({"dataset": fully_qualified_name, "columns": []}, sort_keys=False)
        logger.info("Creating Soda contract for dataset '{}'...", dataset_id)
        response = self._request(
            "POST",
            "/api/v1/contracts",
            json={"datasetId": dataset_id, "contents": contents},
            error_code="CONTRACT_CREATION_FAILED",
            error_context=f"Contract creation for dataset '{dataset_id}'",
        )
        logger.debug(
            "Contract creation response: status={} headers={} body={}",
            response.status_code,
            dict(response.headers),
            _safe_json(response),
        )
        if response.status_code == 400 and _safe_json(response).get("code") == "dataset_contract_already_exists":
            # A contract for this dataset can be created concurrently between the
            # lookup above and this POST (e.g. a concurrent provisioning run). Look
            # it up again and reuse it.
            logger.info(
                "Soda contract for dataset '{}' was created concurrently, looking it up.", dataset_id
            )
            existing_contract_ids = self.find_contract_ids_for_dataset(fully_qualified_name)
            if existing_contract_ids:
                return existing_contract_ids[0]

        if response.status_code not in (200, 201):
            raise SodaApiError(
                "CONTRACT_CREATION_FAILED",
                f"Contract creation for dataset '{dataset_id}' failed with HTTP {response.status_code} "
                f"(response body: {response.content!r}).",
            )

        created_contract_id = (_safe_json(response).get("contract") or {}).get("id")
        if not created_contract_id:
            raise SodaApiError(
                "UNEXPECTED_API_RESPONSE",
                f"Contract creation for dataset '{dataset_id}' did not return a 'contract.id'.",
            )
        contract_id = str(created_contract_id)
        logger.info("Created Soda contract '{}' for dataset '{}'.", contract_id, dataset_id)
        return str(contract_id)

    # ---- Phase 6: Contract Publication ----

    def publish_contract(self, contract_id: str, contents: str) -> None:
        # Publishing new contract YAML content is `POST /api/v1/contracts/{contractId}`
        # with body `{"contents": <yaml>}`. The response wraps the updated
        # contract under a `contract` key and carries no status field: a 200
        # response is the confirmation that the contract was published.
        logger.info("Publishing contract '{}'...", contract_id)
        logger.debug("Contract publish request contents (repr): {!r}", contents)
        publish_response = self._request(
            "POST",
            f"/api/v1/contracts/{contract_id}",
            json={"contents": contents},
            error_code="CONTRACT_PUBLISH_FAILED",
            error_context=f"Contract publish for contract '{contract_id}'",
        )
        logger.debug(
            "Contract publish response: status={} headers={} body={}",
            publish_response.status_code,
            dict(publish_response.headers),
            _safe_json(publish_response),
        )
        if publish_response.status_code != 200:
            raise SodaApiError(
                "CONTRACT_PUBLISH_FAILED",
                f"Contract publish for contract '{contract_id}' failed with HTTP {publish_response.status_code} "
                f"(response body: {publish_response.content!r}).",
            )
        logger.info("Contract '{}' published successfully.", contract_id)

    # ---- Unprovisioning: Contract Lookup and Clearing ----

    def find_contract_ids_for_dataset(self, fully_qualified_name: str) -> List[str]:
        logger.info("Looking up Soda contracts for dataset '{}'...", fully_qualified_name)
        response = self._request(
            "GET",
            "/api/v1/contracts",
            params={"datasetQualifiedName": fully_qualified_name},
            error_code="CONTRACT_API_ERROR",
            error_context=f"Contract lookup for dataset '{fully_qualified_name}'",
        )
        if response.status_code == 404:
            logger.info("No Soda contracts found for dataset '{}' (already unprovisioned).", fully_qualified_name)
            return []
        if response.status_code != 200:
            raise SodaApiError(
                "CONTRACT_API_ERROR",
                f"Contract lookup for dataset '{fully_qualified_name}' failed with HTTP {response.status_code}.",
            )

        items = _extract_items(_safe_json(response))
        contract_ids = [str(item.get("id")) for item in items if item.get("id")]
        logger.info("Found {} Soda contract(s) for dataset '{}'.", len(contract_ids), fully_qualified_name)
        return contract_ids

    def clear_contract(self, contract_id: str, fully_qualified_name: str) -> None:
        # The Soda Cloud Contracts REST API supports listing, creating, and
        # publishing contracts; it does not support deleting a contract
        # resource. Removing the data quality checks associated with a
        # dataset is done by publishing the contract again with an empty
        # skeleton (`{dataset: <fqn>, columns: []}`, no `checks`). The
        # contract resource and the underlying Soda Data Source remain in
        # place; only the check definitions are cleared.
        logger.info("Clearing checks from Soda contract '{}'...", contract_id)
        empty_contents = yaml.safe_dump({"dataset": fully_qualified_name, "columns": []}, sort_keys=False)
        self.publish_contract(contract_id, empty_contents)
        logger.info("Cleared checks from Soda contract '{}'.", contract_id)
