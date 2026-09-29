from pathlib import Path

from starlette.testclient import TestClient

from src.app_config import SodaConfig
from src.main import app
from src.models.api_models import (
    DescriptorKind,
    ProvisioningRequest,
)

client = TestClient(app)


def test_provisioning_invalid_descriptor():
    provisioning_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor="descriptor"
    )

    resp = client.post("/v1/provision", json=provisioning_request.model_dump())

    assert resp.status_code == 400
    assert "Unable to parse the descriptor." in resp.json().get("errors")


def test_provisioning_valid_descriptor():
    # Generic scaffold fixture (Snowflake output port) is not a Soda workload:
    # /v1/provision returns 202 + a task token immediately (descriptor-level
    # discrimination happens inside the background task), and the task then
    # converges to FAILED once polled.
    descriptor_str = Path("tests/descriptors/descriptor_output_port_valid.yaml").read_text()

    provisioning_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor=descriptor_str
    )

    resp = client.post("/v1/provision", json=provisioning_request.model_dump())

    assert resp.status_code == 202
    token = resp.text

    status_resp = client.get(f"/v1/provision/{token}/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "FAILED"
    assert "is not a Soda workload" in body["result"]


def test_provisioning_soda_workload_without_soda_credentials(monkeypatch):
    # Force unconfigured Soda credentials regardless of the developer's
    # ambient shell environment (e.g. real SODA_API_KEY_ID/SODA_API_KEY_SECRET
    # exported for manual end-to-end testing against a real Soda Cloud
    # tenant), so this test deterministically exercises the "no credentials"
    # failure path instead of making a real Soda Cloud API call.
    monkeypatch.setattr(
        "src.services.provision_service.soda_config",
        SodaConfig(api_key_id=None, api_key_secret=None),
    )
    descriptor_str = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()

    provisioning_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor=descriptor_str
    )

    resp = client.post("/v1/provision", json=provisioning_request.model_dump())

    assert resp.status_code == 202
    token = resp.text

    status_resp = client.get(f"/v1/provision/{token}/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "FAILED"
    assert "AUTHENTICATION_FAILED" in body["result"]


def test_get_status_unknown_token():
    resp = client.get("/v1/provision/does-not-exist/status")

    assert resp.status_code == 400
    assert "Unknown task token" in resp.json().get("errors")[0]


def test_unprovisioning_invalid_descriptor():
    unprovisioning_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor="descriptor"
    )

    resp = client.post("/v1/unprovision", json=unprovisioning_request.model_dump())

    assert resp.status_code == 400
    assert "Unable to parse the descriptor." in resp.json().get("errors")


def test_unprovisioning_valid_descriptor():
    # Same discrimination behavior as provisioning: 202 + token immediately,
    # FAILED once polled for a non-Soda component.
    descriptor_str = Path("tests/descriptors/descriptor_output_port_valid.yaml").read_text()

    unprovisioning_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor=descriptor_str
    )

    resp = client.post("/v1/unprovision", json=unprovisioning_request.model_dump())

    assert resp.status_code == 202
    token = resp.text

    status_resp = client.get(f"/v1/provision/{token}/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "FAILED"
    assert "is not a Soda workload" in body["result"]


def test_validate_invalid_descriptor():
    validate_request = ProvisioningRequest(descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor="descriptor")

    resp = client.post("/v1/validate", json=validate_request.model_dump())

    assert resp.status_code == 200
    assert "Unable to parse the descriptor." in resp.json().get("error").get("errors")


def test_validate_valid_descriptor():
    # This fixture describes a Snowflake output port, which is not a Soda workload:
    # its componentIdToProvision does not match the Soda infrastructureTemplateId,
    # so validation must fail with a descriptive error.
    descriptor_str = Path("tests/descriptors/descriptor_output_port_valid.yaml").read_text()

    validate_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor=descriptor_str
    )

    resp = client.post("/v1/validate", json=validate_request.model_dump())

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("valid") is False
    assert "is not a Soda workload" in body.get("error").get("errors")[0]


def test_validate_soda_workload_valid_descriptor():
    descriptor_str = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()

    validate_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor=descriptor_str
    )

    resp = client.post("/v1/validate", json=validate_request.model_dump())

    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "error": None}


def test_validate_soda_workload_missing_output_port():
    descriptor_str = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()
    descriptor_str = descriptor_str.replace(
        "                - urn:dmb:cmp:finance:orders:0:orders-output-port\n",
        "                - urn:dmb:cmp:finance:orders:0:missing-output-port\n",
        1,
    )

    validate_request = ProvisioningRequest(
        descriptorKind=DescriptorKind.COMPONENT_DESCRIPTOR, descriptor=descriptor_str
    )

    resp = client.post("/v1/validate", json=validate_request.model_dump())

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("valid") is False
    assert any("was not found in the Data Product descriptor" in e for e in body.get("error").get("errors"))


def test_updateacl_not_supported():
    # updateAcl is intentionally not implemented: the Soda tech adapter does
    # not manage access grants for any component it provisions. The route
    # must not exist rather than silently succeed or fail.
    resp = client.post("/v1/updateacl", json={})

    assert resp.status_code == 404
