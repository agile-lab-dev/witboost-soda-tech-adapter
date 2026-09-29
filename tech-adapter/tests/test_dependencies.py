import asyncio
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.dependencies import (
    UnpackedProvisioningRequestDep,
    unpack_provisioning_request,
)
from src.models.api_models import (
    ProvisioningRequest,
    ValidationError,
)
from src.models.data_product_descriptor import DataProduct


class TestUnpackProvisioningRequest(unittest.TestCase):
    descriptor_str = Path("tests/descriptors/descriptor_output_port_valid.yaml").read_text()
    provisioning_request = ProvisioningRequest(
        descriptorKind="COMPONENT_DESCRIPTOR",
        descriptor=descriptor_str,  # noqa: E501
    )

    invalid_provisioning_request = ProvisioningRequest(
        # dropped the 'name' field from the previous provisioning_request
        descriptorKind="COMPONENT_DESCRIPTOR",
        descriptor=descriptor_str.replace("name: Vaccinations", "invalid_field: Invalid Value"),  # noqa: E501
    )

    def test_successful_unpack(self):
        result = asyncio.run(unpack_provisioning_request(self.provisioning_request))
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], DataProduct)
        self.assertEqual(result[1], "urn:dmb:cmp:healthcare:vaccinations:0:snowflake-output-port")

    def test_invalid_request(self):
        result = asyncio.run(unpack_provisioning_request(self.invalid_provisioning_request))
        self.assertIsInstance(result, ValidationError)
        self.assertIn("Failed to parse the descriptor. Details:", result.errors[0])

    def test_exception_handling(self):
        provisioning_request = Mock()
        provisioning_request.descriptorKind = "COMPONENT_DESCRIPTOR"
        provisioning_request.descriptor = "Invalid JSON"

        result = asyncio.run(unpack_provisioning_request(provisioning_request))
        self.assertIsInstance(result, ValidationError)


app_test = FastAPI()


@app_test.post("/provision")
async def provision(data: UnpackedProvisioningRequestDep):
    if isinstance(data, tuple) and len(data) == 2:
        data_product, component_id = data
        return {
            "message": "Provisioning completed successfully",
            "data_product": data_product.model_dump(),
            "component_id": component_id,
        }
    elif isinstance(data, ValidationError):
        return {"message": "Provisioning failed", "errors": data.errors}


client = TestClient(app_test)


class TestAppDependenciesMock(unittest.TestCase):
    def test_provision_valid_request(self):
        descriptor_str = Path("tests/descriptors/descriptor_output_port_valid.yaml").read_text()
        valid_provisioning_request = ProvisioningRequest(
            descriptorKind="COMPONENT_DESCRIPTOR",
            descriptor=descriptor_str,  # noqa: E501
        )

        response = client.post("/provision", json=valid_provisioning_request.model_dump())

        assert response.status_code == 200
        assert "Provisioning completed successfully" in response.json()["message"]
        assert "data_product" in response.json()
        assert "component_id" in response.json()

    def test_provision_invalid_request(self):
        invalid_provisioning_request = ProvisioningRequest(
            descriptorKind="COMPONENT_DESCRIPTOR", descriptor="invalid_descriptor"
        )

        response = client.post("/provision", json=invalid_provisioning_request.model_dump())

        assert response.status_code == 200
        assert "Provisioning failed" in response.json()["message"]
        assert "errors" in response.json()
