# Technical Details — Soda Tech Adapter

This document provides the technical implementation details for the Soda Tech Adapter. It complements the [High Level Design](HLD.md) with API-level interactions, polling behavior, error codes, and response structures.

- [Configuration](#configuration)
- [Provisioning](#provisioning)
  - [Phase 0: Output Port Resolution](#phase-0-output-port-resolution)
  - [Phase 1: Data Source Resolution](#phase-1-data-source-resolution)
  - [Phase 2: Discovery](#phase-2-discovery)
  - [Phase 3: Dataset Identification](#phase-3-dataset-identification)
  - [Phase 4: Dataset Onboarding](#phase-4-dataset-onboarding)
  - [Phase 5: Contract Creation](#phase-5-contract-creation)
  - [Phase 6: Contract Publication](#phase-6-contract-publication)
- [Unprovisioning](#unprovisioning)
  - [Contract Lookup and Clearing](#contract-lookup-and-clearing)
- [Validation](#validation)
- [Polling Strategy](#polling-strategy)
- [Error Response Structure](#error-response-structure)
- [Failure Matrix](#failure-matrix)

---

## Configuration

`SodaConfig` (`tech-adapter/src/app_config.py`) is a `pydantic-settings` model with one field per configuration item (`base_url`, `request_timeout_seconds`, `polling_interval_seconds`, `polling_max_timeout_seconds`, `polling_backoff_multiplier`, `polling_backoff_max_interval_seconds`, `api_key_id`, `api_key_secret`). For each field, values are resolved in this order, highest priority first:

1. An explicit constructor argument (used by tests only).
2. An environment variable named `SODA_<FIELD_NAME>` (e.g. `base_url` <-> `SODA_BASE_URL`).
3. The mounted, non-secret `config/application.yaml` file (see `helm/files/application.yaml`, mounted by the Helm chart at that exact relative path under the container's `/app` working directory). Each top-level YAML key matches a `SodaConfig` field name exactly (e.g. `base_url`, `request_timeout_seconds`).
4. The default declared on the `SodaConfig` field.

`api_key_id` and `api_key_secret` (Soda Cloud Basic auth credentials) are always injected as `SODA_API_KEY_ID` / `SODA_API_KEY_SECRET` environment variables from a Kubernetes Secret and must never appear in `application.yaml` or the component descriptor.

## Provisioning

All interactions with Soda Cloud use the REST API, authenticated via Basic auth (`SODA_API_KEY_ID` as username, `SODA_API_KEY_SECRET` as password, both injected as Kubernetes secrets).

> **Component discrimination**: `componentIdToProvision` points at the `workload` component to provision. The adapter only proceeds if that component's `infrastructureTemplateId` equals `urn:dmb:itm:soda-tech-adapter:0`; otherwise it fails immediately with a plain error message. This workload is a **subcomponent nested inside its target Output Port's own `components` list** (not a top-level sibling) — component lookup is recursive, so this works regardless of nesting depth. The Data Product descriptor may also contain a Databricks Guardian workload (owned by the Databricks Tech Adapter, nested one level deeper still), which the Soda adapter never reads or provisions — it only ever looks at `componentIdToProvision` and the Output Ports it points to. See [`docs/reference-descriptor.yaml`](reference-descriptor.yaml) for a fully worked example.

| Phase | Name | Endpoint | Async |
| --- | --- | --- | --- |
| 0 | Output Port Resolution | — (descriptor only) | No |
| 1 | Data Source Resolution | `GET /api/v1/datasources` | No |
| 2 | Discovery | `POST /api/v1/datasources/{id}/discover` + poll `GET /api/v1/scans/{id}` | Yes |
| 3 | Dataset Identification | `GET /api/v1/discoveredDatasets` | No |
| 4 | Dataset Onboarding | `POST /api/v1/datasources/{id}/onboardDatasets` + status poll | Yes |
| 5 | Contract Creation | `GET /api/v1/contracts` (lookup, idempotent reuse) + `POST /api/v1/contracts` | No |
| 6 | Contract Publication | `POST /api/v1/contracts/{contractId}` | No |

### Phase 0: Output Port Resolution

```text
1. Read the single platform key under the workload's `specific` (e.g. `specific.databricks`),
   containing `dataSourceName` and `outputPortIds`
2. Find each Output Port by ID in the Data Product descriptor
3. Extract: table metadata (catalog, schema, table), data contract checks from dataContract.quality[]
   Each quality[] entry is a QualityCheck: { check: <Soda Contract Language YAML fragment>, column?: <column name> }
   `check` is a single-key YAML mapping representing one Contract Language check type
   (e.g. "row_count:\n  threshold:\n    must_be_greater_than: 0\n").
   Checks with a `column` are placed under that column's `checks` list in the built contract;
   checks without a `column` are placed at the dataset level.
4. Build: fully qualified table name from dataSourceName + table metadata
5. On a referenced Output Port ID that does not exist in the descriptor, or that is not an
   Output Port component → provisioning fails with a plain, human-readable error message.
6. On a resolved Output Port with no table name (`viewNameOP`/`tableName` both absent) →
   FAILURE (OUTPUT_PORT_NOT_FOUND)
```

### Phase 1: Data Source Resolution

```text
1. GET /api/v1/datasources?search=<dataSourceName>
2. Validate: exactly 1 result
3. Extract: datasourceId
4. On non-200 response → FAILURE (DATA_SOURCE_API_ERROR)
5. On 0 results → FAILURE (DATA_SOURCE_NOT_FOUND)
6. On >1 results → FAILURE (DATA_SOURCE_AMBIGUOUS)
7. On a result missing an `id` field → FAILURE (UNEXPECTED_API_RESPONSE)
```

### Phase 2: Discovery

```text
1. POST /api/v1/datasources/{datasourceId}/discover
2. Extract: scanId from X-Soda-Scan-Id header
   On trigger failure (non-2xx) or a missing header → FAILURE (DISCOVERY_TRIGGER_FAILED,
   or UNEXPECTED_API_RESPONSE if the header is missing)
3. Poll: GET /api/v1/scans/{scanId}
   On a non-200 response while polling → FAILURE (DISCOVERY_TRIGGER_FAILED)
4. Wait for terminal state:
   - completed → continue
   - completedWithWarnings → continue (warnings are non-blocking)
   - completedWithErrors → FAILURE
   - completedWithFailures → FAILURE
   - failed → FAILURE
   - timedOut → FAILURE
   - canceled → FAILURE
5. On polling timeout → FAILURE (DISCOVERY_POLLING_TIMEOUT)
```

**Discovery state interpretation:**

| Soda State | Provisioning Interpretation | Rationale |
| --- | --- | --- |
| `completed` | Continue | Discovery succeeded |
| `completedWithWarnings` | Continue | Warnings do not prevent dataset availability |
| `completedWithErrors` | FAILURE | Errors may indicate the target dataset was not discovered |
| `completedWithFailures` | FAILURE | Failures indicate critical issues in discovery |
| `failed` | FAILURE | Discovery process failed |
| `timedOut` | FAILURE | Discovery did not complete within Soda's internal timeout |
| `canceled` | FAILURE | Discovery was externally canceled |

### Phase 3: Dataset Identification

```text
1. GET /api/v1/discoveredDatasets?datasourceId=<id>&search=<table>
2. Filter results by fully qualified name: <dataSourceName>/<catalog>/<schema>/<table>
3. Validate: exactly 1 exact match
4. Extract: discoveredDatasetId
5. On non-200 response → FAILURE (DATASET_API_ERROR)
6. On 0 matches → FAILURE (DATASET_NOT_FOUND)
7. On >1 matches after FQ filtering → FAILURE (DATASET_AMBIGUOUS)
8. On a match missing an `id` field → FAILURE (UNEXPECTED_API_RESPONSE)
```

The matching uses the fully qualified name to prevent false positives:

```text
Expected:  databricks-production/main/sales/orders
Reject:    databricks-production/main/staging/orders  (different schema)
Reject:    databricks-production/archive/sales/orders (different catalog)
```

### Phase 4: Dataset Onboarding

```text
1. POST /api/v1/datasources/{datasourceId}/onboardDatasets
   Body: { discoveredDatasetIds: [discoveredDatasetId] }
2. Extract: the polling URL from the response's `Location` header, and an operationId
   (from the response body's `operationId` field if present, otherwise from the last
   segment of the `Location` URL)
3. Poll: GET <Location URL>
4. Wait for terminal state:
   - completed / completedWithWarnings → continue (the discovered dataset id is reused
     as the onboarded dataset id; Soda Cloud does not mint a separate id during onboarding)
   - completedWithErrors → FAILURE (ONBOARDING_COMPLETED_WITH_ERRORS)
   - completedWithFailures → FAILURE (ONBOARDING_COMPLETED_WITH_FAILURES)
   - failed → FAILURE (ONBOARDING_FAILED)
   - timedOut → FAILURE (ONBOARDING_TIMED_OUT)
   - canceled → FAILURE (ONBOARDING_CANCELED)
5. On polling timeout → FAILURE (ONBOARDING_POLLING_TIMEOUT)
6. On a 200 response with an empty/unparsable body while polling → FAILURE (UNEXPECTED_API_RESPONSE)
```

### Phase 5: Contract Creation

Soda Cloud allows **at most one contract per dataset**: a second `POST /api/v1/contracts` for a dataset that already has one is rejected with HTTP 400 `dataset_contract_already_exists`. Contract creation looks up an existing contract for the dataset and reuses it when present, so re-running provisioning for an already-provisioned Output Port succeeds and reuses the existing contract.

```text
1. GET /api/v1/contracts?datasetQualifiedName=<FQN>
2. If a contract is found for the dataset → reuse its id, skip creation, continue to Phase 6
3. Otherwise:
   a. POST /api/v1/contracts
      Body: { datasetId: <onboardedDatasetId>, contents: "dataset: <FQN>\ncolumns: []\n" }
      (`contents` is required at creation time; a minimal skeleton is sent here)
   b. Extract: contractId from response body `{"contract": {"id": ..., ...}}`
   c. On HTTP 400 with code `dataset_contract_already_exists` (a contract for this dataset
      was created concurrently between step 1 and 3a) → re-run step 1 and reuse the found id
   d. On other failure → FAILURE (CONTRACT_CREATION_FAILED)
   e. On missing `contract.id` in response → FAILURE (UNEXPECTED_API_RESPONSE)
```

### Phase 6: Contract Publication

```text
1. Build contract contents as a Soda Contract Language YAML document (see
   https://docs.soda.io/reference/contract-language-reference) from the Output Port's
   dataContract.quality[] checks:
   - dataset: <FQN>
   - checks: [<dataset-level QualityCheck.check mappings, i.e. checks without a `column`>]
   - columns: [{ name: <column>, checks: [<column-level QualityCheck.check mappings>] }, ...]
2. POST /api/v1/contracts/{contractId}
   Body: { contents: <contract YAML> }
3. A 200 response is treated as confirmation that the contract was published; the response
   body is not parsed by the adapter (only logged for debugging)
4. On publish failure (non-200) → FAILURE (CONTRACT_PUBLISH_FAILED)
```

> **Note**: `POST /v1/provision` executes Phases 1-6 without first running the descriptor validation performed by `POST /v1/validate` (see [Validation](#validation)). A malformed `dataContract.quality[].check` is caught here, at contract publish time, as a Soda Cloud-side HTTP 400. Running that validation synchronously at the start of `POST /v1/provision` (to fail fast with an adapter-side error instead) is a possible improvement, not yet implemented.
>
> The Contracts REST API also exposes `POST /api/v1/contracts/{contractId}/verify` to trigger an on-demand verification scan against the published contract. Provisioning creates and publishes the contract; scan scheduling and verification are handled by Soda Cloud's own configuration.

---

## Unprovisioning

The Soda Cloud Contracts REST API supports listing, creating, and publishing contracts; it does not support deleting a contract resource. Removing the data quality checks associated with a dataset is done by publishing the contract again with an empty skeleton (see [Phase 6](#phase-6-contract-publication)).

Unprovisioning clears the checks of each Output Port's Soda Data Contract by publishing it with an empty skeleton (`dataset: <FQN>`, `columns: []`, no `checks`). The contract resource, the dataset, and the underlying Soda Data Source remain in place; only the check definitions are removed. The operation is idempotent: if no contract exists for the dataset, the step is skipped without error.

> The Soda Data Source is never removed during unprovisioning — it is pre-provisioned shared infrastructure.

### Contract Lookup and Clearing

```text
1. Extract table metadata from descriptor and build FQN: <dataSourceName>/<catalog>/<schema>/<table>
2. GET /api/v1/contracts?datasetQualifiedName=<FQN>
   (an HTTP 404 response is treated the same as an empty result: no contracts to clear)
3. For each contract found:
   a. POST /api/v1/contracts/{contractId}
      Body: { contents: "dataset: <FQN>\ncolumns: []\n" }
   b. On failure (non-200) → FAILURE (CONTRACT_PUBLISH_FAILED)
4. If no contracts found → success (nothing to clear)
```

On success, the response's `info.publicInfo.sodaUnprovisioningResult` lists, for each cleared contract, `{ outputPortId, sodaContractId }`.

> The dataset is fully managed by Witboost. All data contracts associated with a dataset have their checks cleared during unprovision; the contract resources and the underlying dataset/Data Source are left in place.

---

## Validation

The `validate` operation (`POST /v1/validate`) performs descriptor-only checks synchronously, before any Soda Cloud API call is made. It returns HTTP 200 with a `ValidationResult`: `{"valid": true}` on success, or `{"valid": false, "error": {"errors": [<message>, ...]}}` on failure. Validation failures are reported as human-readable messages, not as symbolic failure codes.

| Check | Field |
| --- | --- |
| Data Source name present | The platform-specific `dataSourceName` (e.g. `specific.databricks.dataSourceName`) must be a non-empty string |
| At least one Output Port | The platform-specific `outputPortIds` (e.g. `specific.databricks.outputPortIds`) must be a non-empty list |
| Output Port exists in descriptor | Each ID in `outputPortIds` must match an Output Port component in the DP descriptor |
| Table metadata complete | Each resolved Output Port must have non-empty catalog/schema/table (preferring `catalogNameOP`/`schemaNameOP`/`viewNameOP`, falling back to `catalogName`/`schemaName`/`tableName`) |
| Data contract present | Each Output Port must have a `dataContract.quality` list with at least one `check` entry |
| Contract Language check syntax | Each `quality[].check` must be valid YAML, a mapping with exactly one key, and that key must be a recognized Contract Language check type (`schema`, `row_count`, `freshness`, `missing`, `invalid`, `duplicate`, `aggregate`, `metric`, `failed_rows`, `group_by`, `reconciliation`) |

---

## Polling Strategy

Two asynchronous Soda Cloud operations require polling: **Discovery** and **Onboarding**.

### Configuration

| Parameter | Default | Description |
| --- | --- | --- |
| `polling_interval_seconds` | 5 seconds | Time between consecutive poll requests |
| `polling_max_timeout_seconds` | 300 seconds (5 min) | Maximum total time to wait for completion |
| `polling_backoff_multiplier` | 1.5 | Exponential backoff multiplier |
| `polling_backoff_max_interval_seconds` | 30 seconds | Maximum interval after backoff |

These are `SodaConfig` fields (see [Configuration](#configuration)) and can be overridden via `SODA_POLLING_INTERVAL_SECONDS`, `SODA_POLLING_MAX_TIMEOUT_SECONDS`, `SODA_POLLING_BACKOFF_MULTIPLIER`, `SODA_POLLING_BACKOFF_MAX_INTERVAL_SECONDS` or `application.yaml`.

### Algorithm

```python
interval = polling_interval_seconds
elapsed = 0

while elapsed < polling_max_timeout_seconds:
    response = GET status_endpoint
    if response.status == HTTP 429:
        wait = response.headers["Retry-After"] or interval * backoff_multiplier
        sleep(wait)
        elapsed += wait
        continue
    if response.status is terminal_success:
        return SUCCESS
    if response.status is terminal_failure:
        return FAILURE(response)
    sleep(interval)
    elapsed += max(interval, 0.01)  # floor guards against a misconfigured 0-second interval
    interval = min(interval * backoff_multiplier, polling_backoff_max_interval_seconds)

return FAILURE(POLLING_TIMEOUT)
```

If the `Retry-After` header is present but not a valid number, it is ignored and the backoff-based wait time is used instead.

### Timeout Configuration

| Operation | Default Timeout | Configurable |
| --- | --- | --- |
| HTTP request timeout | 30 seconds | Yes |
| Discovery polling max | 300 seconds | Yes |
| Onboarding polling max | 300 seconds | Yes |

There is no separate overall-provisioning timeout: with multiple Output Ports, each one's phases run one after another, and the only bounds are the per-request HTTP timeout and the per-operation polling timeout above.

### Rate Limiting

The adapter does not enforce or hard-code a specific Soda Cloud rate-limit quota. If Soda Cloud returns HTTP 429 on a polling request, the adapter waits (honoring `Retry-After` when present, otherwise using the backoff interval) and retries; this wait can be sub-second if `Retry-After` specifies a fractional value.

---

## Error Response Structure

The Tech Adapter returns responses conforming to the [Witboost Tech Adapter API](https://docs.witboost.com/docs/apis/extension-points/control-plane/builder/tech-adapter-api) `ProvisioningStatus` schema (`status`, `result`, `info`), returned by `GET /v1/provision/{token}/status` (shared by both provisioning and unprovisioning tokens).

**Successful provisioning:**

```json
{
  "status": "COMPLETED",
  "result": "",
  "info": {
    "publicInfo": {
      "sodaProvisioningResult": [
        {
          "outputPortId": "urn:dmb:cmp:finance:orders:0:orders-output-port",
          "sodaContractId": "contract-abc-123",
          "sodaDatasetId": "dataset-xyz-789",
          "dataSourceName": "databricks-production"
        },
        {
          "outputPortId": "urn:dmb:cmp:finance:orders:0:customers-output-port",
          "sodaContractId": "contract-def-456",
          "sodaDatasetId": "dataset-uvw-012",
          "dataSourceName": "databricks-production"
        }
      ]
    },
    "privateInfo": {}
  }
}
```

**Successful unprovisioning:**

```json
{
  "status": "COMPLETED",
  "result": "",
  "info": {
    "publicInfo": {
      "sodaUnprovisioningResult": [
        {
          "outputPortId": "urn:dmb:cmp:finance:orders:0:orders-output-port",
          "sodaContractId": "contract-abc-123"
        }
      ]
    },
    "privateInfo": {}
  }
}
```

**Failed provisioning/unprovisioning:**

```json
{
  "status": "FAILED",
  "result": "Provisioning failed for Output Port 'orders-output-port' (DISCOVERY_POLLING_TIMEOUT): Discovery scan 'scan-abc-123' did not complete within 300.0s.",
  "info": null
}
```

`result` is a single human-readable message; it does not carry a separate structured failure code (see [Failure Matrix](#failure-matrix) for the codes embedded in the message).

**Running (in-progress):**

```json
{
  "status": "RUNNING",
  "result": "",
  "info": null
}
```

The synchronous `POST /v1/validate` endpoint uses a different, dedicated schema instead — see [Validation](#validation).

---

## Failure Matrix

| # | Phase | Failure | Code | Action |
| --- | --- | --- | --- | --- |
| 1 | Output Port | Referenced Output Port ID not found in descriptor, or not an Output Port | *(none — plain error message)* | Verify the Output Port ID exists in the Data Product descriptor and is of kind `outputport` |
| 2 | Output Port | Resolved table name missing (`viewNameOP`/`tableName` both absent) | `OUTPUT_PORT_NOT_FOUND` | Ensure the Output Port's `specific` has `viewNameOP`/`tableName` |
| 3 | Data Source | Not found | `DATA_SOURCE_NOT_FOUND` | Verify Data Source name in Soda Cloud configuration |
| 4 | Data Source | Ambiguous (>1 match) | `DATA_SOURCE_AMBIGUOUS` | Use a more specific Data Source name |
| 5 | Data Source | HTTP error | `DATA_SOURCE_API_ERROR` | Verify Soda Cloud API connectivity and credentials |
| 6 | Discovery | Trigger HTTP failure, or scan-status lookup HTTP error | `DISCOVERY_TRIGGER_FAILED` | Verify Soda Cloud API connectivity and Data Source configuration |
| 7 | Discovery | Scan status: `failed` | `DISCOVERY_FAILED` | Check Soda Agent logs; verify Data Source connectivity |
| 8 | Discovery | Scan status: `timedOut` | `DISCOVERY_TIMED_OUT` | Check Soda Agent performance; increase Soda-side timeout if needed |
| 9 | Discovery | Scan status: `canceled` | `DISCOVERY_CANCELED` | Investigate external cancellation cause |
| 10 | Discovery | Scan status: `completedWithErrors` | `DISCOVERY_COMPLETED_WITH_ERRORS` | Open the `cloudUrl` link included in the error message to inspect the scan in Soda Cloud |
| 11 | Discovery | Scan status: `completedWithFailures` | `DISCOVERY_COMPLETED_WITH_FAILURES` | Open the `cloudUrl` link included in the error message to inspect the scan in Soda Cloud |
| 12 | Discovery | Polling timeout exceeded | `DISCOVERY_POLLING_TIMEOUT` | Increase `polling_max_timeout_seconds` |
| 13 | Dataset | Not found after discovery | `DATASET_NOT_FOUND` | Verify table exists on the target data platform |
| 14 | Dataset | Ambiguous match | `DATASET_AMBIGUOUS` | Review fully qualified name matching logic |
| 15 | Dataset | HTTP error | `DATASET_API_ERROR` | Verify Soda Cloud API connectivity |
| 16 | Onboarding | Trigger HTTP failure | `ONBOARDING_TRIGGER_FAILED` | Verify Soda Cloud API connectivity |
| 17 | Onboarding | Operation status: `failed` | `ONBOARDING_FAILED` | Check onboarding errors in Soda Cloud |
| 18 | Onboarding | Operation status: `completedWithErrors` | `ONBOARDING_COMPLETED_WITH_ERRORS` | Check onboarding errors in Soda Cloud |
| 19 | Onboarding | Operation status: `completedWithFailures` | `ONBOARDING_COMPLETED_WITH_FAILURES` | Check onboarding failures in Soda Cloud |
| 20 | Onboarding | Operation status: `timedOut` | `ONBOARDING_TIMED_OUT` | Check Soda Cloud performance; increase Soda-side timeout if needed |
| 21 | Onboarding | Operation status: `canceled` | `ONBOARDING_CANCELED` | Investigate external cancellation cause |
| 22 | Onboarding | Polling timeout exceeded | `ONBOARDING_POLLING_TIMEOUT` | Increase `polling_max_timeout_seconds` |
| 23 | Onboarding | HTTP error | `ONBOARDING_API_ERROR` | Verify Soda Cloud API connectivity |
| 24 | Contract | Creation failed | `CONTRACT_CREATION_FAILED` | Check payload validity |
| 25 | Contract | Publish failed (also used for unprovision check-clearing) | `CONTRACT_PUBLISH_FAILED` | Check contract YAML validity and contract state in Soda Cloud |
| 26 | Contract | HTTP error (e.g. lookup) | `CONTRACT_API_ERROR` | Verify Soda Cloud API connectivity |
| 27 | General | Unexpected API response (e.g. missing `id`/`contract.id`, or an unparsable onboarding status body) | `UNEXPECTED_API_RESPONSE` | Investigate; possible API version mismatch |
| 28 | General | HTTP 401/403 on any Soda Cloud call, or missing API key configuration | `AUTHENTICATION_FAILED` | Verify API key permissions and that `SODA_API_KEY_ID`/`SODA_API_KEY_SECRET` are set |

> Validation failures (`POST /v1/validate`) are not reported through the codes above: they are returned as plain, human-readable messages in `ValidationResult.error.errors` (see [Validation](#validation)). See [Phase 6: Contract Publication](#phase-6-contract-publication) for why a malformed check surfaces as `CONTRACT_PUBLISH_FAILED` at provisioning time instead of an adapter-side validation error.
