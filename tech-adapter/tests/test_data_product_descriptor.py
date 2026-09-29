import unittest
from pathlib import Path

import pydantic_core
import pytest
import yaml

from src.models.data_product_descriptor import (
    ComponentKind,
    ConnectionTypeWorkload,
    DataContract,
    DataProduct,
    DataSharingAgreement,
    Observability,
    OpenMetadataColumn,
    OutputPort,
    StorageArea,
    Workload,
)
from src.utility.parsing_pydantic_models import parse_yaml_with_model


class TestDataProductDescriptor(unittest.TestCase):
    def setUp(self):
        # Create sample OpenMetadataColumn instances for the schema
        column1 = OpenMetadataColumn(name="column1", dataType="string")
        column2 = OpenMetadataColumn(name="column2", dataType="int")

        # Create a sample DataProduct object for testing
        self.sample_data_product = DataProduct(
            id="1",
            name="Sample Data Product",
            description="A test data product",
            kind="dataproduct",
            domain="Sample Domain",
            version="1.0",
            environment="Development",
            dataProductOwner="John Doe",
            ownerGroup="Data Owners",
            devGroup="Development Team",
            specific={},
            components=[
                OutputPort(
                    id="op1",
                    name="Output Port 1",
                    description="An output port",
                    specific={},
                    kind=ComponentKind.OUTPUTPORT,
                    version="1.0",
                    infrastructureTemplateId="infra1",
                    outputPortType="Type1",
                    dependsOn=["op2"],  # Depends on another output port within the same Data Product
                    dataContract=DataContract(schema=[column1, column2]),  # Provide valid schema instances
                    dataSharingAgreement=DataSharingAgreement(),
                    tags=[],
                    semanticLinking=[],
                ),
                OutputPort(
                    id="op2",
                    name="Output Port 2",
                    description="Another output port",
                    specific={},
                    kind=ComponentKind.OUTPUTPORT,
                    version="1.0",
                    infrastructureTemplateId="infra2",
                    outputPortType="Type2",
                    dependsOn=[],  # No dependencies
                    dataContract=DataContract(schema=[]),
                    dataSharingAgreement=DataSharingAgreement(),
                    tags=[],
                    semanticLinking=[],
                ),
                Workload(
                    id="wl1",
                    name="Workload 1",
                    description="A workload",
                    specific={},
                    kind=ComponentKind.WORKLOAD,
                    version="1.0",
                    infrastructureTemplateId="infra2",
                    connectionType=ConnectionTypeWorkload.HOUSEKEEPING,
                    dependsOn=[],
                    tags=[],
                ),
                StorageArea(
                    id="sa1",
                    name="Storage Area 1",
                    description="A storage area",
                    specific={},
                    kind=ComponentKind.STORAGE,
                    owners=["Owner1"],
                    infrastructureTemplateId="infra3",
                    dependsOn=[],
                    tags=[],
                ),
                Observability(
                    id="obs1",
                    name="Observability 1",
                    description="An observability component",
                    specific={},
                    kind=ComponentKind.OBSERVABILITY,
                    endpoint="http://example.com",
                    completeness={},
                    dataProfiling={},
                    freshness={},
                    availability={},
                    dataQuality={},
                    tags=[],
                ),
            ],
            tags=[],
        )

    def test_get_components_by_kind_outputport(self):
        output_ports = self.sample_data_product.get_components_by_kind(ComponentKind.OUTPUTPORT)
        self.assertEqual(2, len(output_ports))
        self.assertIsInstance(output_ports[0], OutputPort)

    def test_get_components_by_kind_workload(self):
        workloads = self.sample_data_product.get_components_by_kind(ComponentKind.WORKLOAD)
        self.assertEqual(1, len(workloads))
        self.assertIsInstance(workloads[0], Workload)

    def test_get_components_by_kind_storage(self):
        storage_areas = self.sample_data_product.get_components_by_kind(ComponentKind.STORAGE)
        self.assertEqual(1, len(storage_areas))
        self.assertIsInstance(storage_areas[0], StorageArea)

    def test_get_components_by_kind_observability(self):
        observability_apis = self.sample_data_product.get_components_by_kind(ComponentKind.OBSERVABILITY)
        self.assertEqual(1, len(observability_apis))
        self.assertIsInstance(observability_apis[0], Observability)

    def test_get_component_by_id_existing(self):
        component_id = "op1"
        component = self.sample_data_product.get_component_by_id(component_id)
        self.assertIsNotNone(component)
        self.assertEqual(component.id, component_id)

    def test_get_component_by_id_non_existing(self):
        component_id = "nonexistent"
        component = self.sample_data_product.get_component_by_id(component_id)
        self.assertIsNone(component)

    def test_get_components_by_kind_outputport_with_dependencies(self):
        output_ports = self.sample_data_product.get_components_by_kind(ComponentKind.OUTPUTPORT)
        self.assertEqual(2, len(output_ports))
        self.assertIsInstance(output_ports[0], OutputPort)
        self.assertIsInstance(output_ports[1], OutputPort)
        self.assertIn("op1", [op.id for op in output_ports])
        self.assertIn("op2", [op.id for op in output_ports])

    def test_get_output_ports(self):
        output_ports = self.sample_data_product.get_output_ports()
        self.assertEqual(2, len(output_ports))
        self.assertIsInstance(output_ports[0], OutputPort)
        self.assertIn("op1", [op.id for op in output_ports])
        self.assertIn("op2", [op.id for op in output_ports])

    def test_get_workloads(self):
        workloads = self.sample_data_product.get_workloads()
        self.assertEqual(1, len(workloads))
        self.assertIsInstance(workloads[0], Workload)
        self.assertEqual("wl1", workloads[0].id)

    def test_get_storage_areas(self):
        storage_areas = self.sample_data_product.get_storage_areas()
        self.assertEqual(1, len(storage_areas))
        self.assertIsInstance(storage_areas[0], StorageArea)
        self.assertEqual("sa1", storage_areas[0].id)

    def test_get_observability_APIs(self):
        observability_apis = self.sample_data_product.get_observability_APIs()
        self.assertEqual(1, len(observability_apis))
        self.assertIsInstance(observability_apis[0], Observability)
        self.assertEqual("obs1", observability_apis[0].id)

    def test_output_port_check_kind_classmethod(self):
        valid_output_port_data = """
            id: output_port_1
            name: Output Port 1
            description: Description for Output Port 1
            specific: {}
            kind: outputport
        """

        valid_output_port_data = yaml.safe_load(valid_output_port_data)

        OutputPort.check_kind(ComponentKind.OUTPUTPORT, valid_output_port_data)  # Should not raise an error

        invalid_output_port_data = """
            id: output_port_2
            name: Output Port 2
            description: Description for Output Port 2
            specific: {}
            kind: workload  # Invalid kind
        """

        invalid_output_port_data = yaml.safe_load(invalid_output_port_data)
        with pytest.raises(ValueError):
            OutputPort.check_kind(ComponentKind.WORKLOAD, invalid_output_port_data)

    def test_workload_check_kind_classmethod(self):
        valid_workload_data = """
          id: workload_1
          name: Workload 1
          description: Description for Workload 1
          specific: {}
          kind: workload
        """

        valid_workload_data = yaml.safe_load(valid_workload_data)
        Workload.check_kind(ComponentKind.WORKLOAD, valid_workload_data)  # Should not raise an error

        invalid_workload_data = """
          id: workload_2
          name: Workload 2
          description: Description for Workload 2
          specific: {}
          kind: outputport  # Invalid kind
        """

        invalid_workload_data = yaml.safe_load(invalid_workload_data)
        with pytest.raises(ValueError):
            Workload.check_kind(ComponentKind.OUTPUTPORT, invalid_workload_data)

    def test_storage_area_check_kind_classmethod(self):
        valid_storage_area_data = """
              id: storage_1
              name: Storage 1
              description: Description for Storage 1
              specific: {}
              kind: STORAGE
        """

        valid_storage_area_data = yaml.safe_load(valid_storage_area_data)
        StorageArea.check_kind(ComponentKind.STORAGE, valid_storage_area_data)  # Should not raise an error

        invalid_storage_area_data = """
            id: storage_2
            name: Storage 2
            description: Description for Storage 2
            specific: {}
            kind: WORKLOAD  # Invalid kind

        """

        invalid_storage_area_data = yaml.safe_load(invalid_storage_area_data)
        with pytest.raises(ValueError):
            StorageArea.check_kind(ComponentKind.WORKLOAD, invalid_storage_area_data)

    def test_observability_check_kind_classmethod(self):
        valid_observability_data = """
        id: observability_1
        name: Observability 1
        description: Description for Observability 1
        specific: {}
        kind: OBSERVABILITY
        """

        valid_observability_data = yaml.safe_load(valid_observability_data)
        Observability.check_kind(ComponentKind.OBSERVABILITY, valid_observability_data)  # Should not raise an error

        invalid_observability_data = """
        id: observability_2
        name: Observability 2
        description: Description for Observability 2
        specific: {}
        kind: WORKLOAD  # Invalid kind
        """

        invalid_observability_data = yaml.safe_load(invalid_observability_data)

        with pytest.raises(ValueError):
            Observability.check_kind(ComponentKind.WORKLOAD, invalid_observability_data)

    def test_open_metadata_column_check_dataType_classmethod(self):
        valid_column_data = """
        name: column1
        dataType: string
        """

        valid_column_data = yaml.safe_load(valid_column_data)

        OpenMetadataColumn.check_dataType("string", valid_column_data)  # Should not raise an error

        invalid_column_data = """
        name: column2
        dataType: invalid_type  # Invalid dataType

        """

        invalid_column_data = yaml.safe_load(invalid_column_data)

        with pytest.raises(ValueError):
            OpenMetadataColumn.check_dataType("invalid_type", invalid_column_data)

    def test_workload_without_readsFrom(self):
        input_data = """
          id: "123"
          name: "Workload1"
          fullyQualifiedName: "example.Workload1"
          description: "Description"
          specific:
            version: "1.0"
          kind: "workload"
          version: "1.0"
          infrastructureTemplateId: "template1"
          dependsOn:
            - "dependency1"
            - "dependency2"
          connectionType: "DATAPIPELINE"
          tags: []
        """

        result = parse_yaml_with_model(input_data, Workload)
        assert isinstance(result, Workload)

    def test_get_typed_component_output_port(self):
        descriptor_str = Path("tests/descriptors/descriptor_output_port_valid.yaml").read_text()
        request = yaml.safe_load(descriptor_str)
        data_product = parse_yaml_with_model(request.get("dataProduct"), DataProduct)
        component_to_provision = request.get("componentIdToProvision")

        assert data_product.get_typed_component_by_id(component_to_provision, OutputPort) is not None

        descriptor_str = Path("tests/descriptors/descriptor_storage_valid.yaml").read_text()
        request = yaml.safe_load(descriptor_str)
        data_product = parse_yaml_with_model(request.get("dataProduct"), DataProduct)
        invalid_component_to_provision = request.get("componentIdToProvision")

        with pytest.raises(pydantic_core.ValidationError, match="4 validation errors for OutputPort"):
            data_product.get_typed_component_by_id(invalid_component_to_provision, OutputPort)

    def test_get_component_by_id_finds_nested_subcomponent(self):
        """
        The current output-port + Soda workload templates nest the Soda
        workload inside the output port's own `components` list, rather than
        as a top-level sibling. `get_component_by_id` must resolve it by its
        own id regardless of nesting depth.
        """
        descriptor_str = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()
        request = yaml.safe_load(descriptor_str)
        data_product = parse_yaml_with_model(request.get("dataProduct"), DataProduct)
        component_to_provision = request.get("componentIdToProvision")

        nested_component = data_product.get_component_by_id(component_to_provision)
        self.assertIsNotNone(nested_component)
        self.assertEqual(nested_component.id, component_to_provision)
        self.assertIsInstance(nested_component, Workload)

        # And it isn't a top-level component of the Data Product.
        self.assertNotIn(component_to_provision, [c.id for c in data_product.components])

    def test_get_component_by_id_nested_not_found(self):
        descriptor_str = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()
        request = yaml.safe_load(descriptor_str)
        data_product = parse_yaml_with_model(request.get("dataProduct"), DataProduct)

        self.assertIsNone(data_product.get_component_by_id("urn:dmb:cmp:finance:orders:0:does-not-exist"))


class TestDataContractODCSNormalization(unittest.TestCase):
    """
    Covers the ODCS v3.2 `dataContract.schema` shape emitted by the current
    output-port templates: `schema` is a list of tables, each carrying
    `properties` (columns) and its own `quality` list of
    `{id, type, engine, implementation}`, instead of the legacy flat
    `schema` (list of columns) + top-level `quality` (list of
    `{check, column}`).
    """

    def test_flattens_odcs_columns_and_dataset_level_check(self):
        data_contract = DataContract(
            schema=[
                {
                    "name": "orders_view",
                    "properties": [
                        {"name": "id", "physicalType": "INT", "description": "id column"},
                        {"name": "name", "physicalType": "VARCHAR(100)", "description": "name column"},
                    ],
                    "quality": [
                        {
                            "id": "row_count_1",
                            "type": "custom",
                            "engine": "soda",
                            "implementation": "row_count:\n  threshold:\n    must_be_greater_than: 15\n",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(2, len(data_contract.schema_))
        self.assertEqual("id", data_contract.schema_[0].name)
        self.assertEqual("INT", data_contract.schema_[0].dataType)
        self.assertEqual("VARCHAR", data_contract.schema_[1].dataType)
        self.assertEqual(100, data_contract.schema_[1].dataLength)

        self.assertEqual(1, len(data_contract.quality))
        self.assertIsNone(data_contract.quality[0].column)
        parsed_check = yaml.safe_load(data_contract.quality[0].check)
        self.assertEqual({"row_count": {"threshold": {"must_be_greater_than": 15}}}, parsed_check)

    def test_flattens_odcs_string_tags_into_tag_labels(self):
        """
        ODCS v3.2 `properties[].tags` may be plain strings (e.g.
        `tags: [bt1, bt2]`), unlike the legacy `OpenMetadataColumn.tags`
        shape which requires `{tagFQN, ...}` objects. The normalizer must
        wrap plain strings as `{"tagFQN": <value>}` rather than raising a
        validation error.
        """
        data_contract = DataContract(
            schema=[
                {
                    "name": "orders_view",
                    "properties": [
                        {
                            "name": "id",
                            "physicalType": "INT",
                            "tags": ["bt1", "bt2"],
                        }
                    ],
                    "quality": [],
                }
            ]
        )

        self.assertEqual(1, len(data_contract.schema_))
        tags = data_contract.schema_[0].tags
        self.assertEqual(2, len(tags))
        self.assertEqual("bt1", tags[0].tagFQN)
        self.assertEqual("bt2", tags[1].tagFQN)

    def test_passes_through_legacy_object_shaped_tags(self):
        """
        Legacy/OpenMetadata-style `{tagFQN, source, labelType, state}` tag
        objects (not part of the ODCS standard, but supported for backward
        compatibility) must still be accepted unchanged alongside plain ODCS
        string tags.
        """
        data_contract = DataContract(
            schema=[
                {
                    "name": "orders_view",
                    "properties": [
                        {
                            "name": "id",
                            "physicalType": "INT",
                            "tags": [
                                {
                                    "tagFQN": "PII",
                                    "source": "Classification",
                                    "labelType": "Manual",
                                    "state": "Confirmed",
                                },
                                "bt2",
                            ],
                        }
                    ],
                    "quality": [],
                }
            ]
        )

        tags = data_contract.schema_[0].tags
        self.assertEqual(2, len(tags))
        self.assertEqual("PII", tags[0].tagFQN)
        self.assertEqual("bt2", tags[1].tagFQN)

    def test_extracts_embedded_column_from_implementation(self):
        data_contract = DataContract(
            schema=[
                {
                    "name": "orders_view",
                    "properties": [{"name": "name", "physicalType": "VARCHAR(100)"}],
                    "quality": [
                        {
                            "id": "duplicate_count_2",
                            "type": "custom",
                            "engine": "soda",
                            "implementation": (
                                "duplicate:\n"
                                "  threshold:\n"
                                "    must_be_less_than_or_equal: 2\n"
                                "column: name\n"
                            ),
                        }
                    ],
                }
            ]
        )

        self.assertEqual(1, len(data_contract.quality))
        self.assertEqual("name", data_contract.quality[0].column)
        parsed_check = yaml.safe_load(data_contract.quality[0].check)
        self.assertEqual({"duplicate": {"threshold": {"must_be_less_than_or_equal": 2}}}, parsed_check)
        self.assertNotIn("column", parsed_check)

    def test_legacy_flat_shape_still_supported(self):
        data_contract = DataContract(
            schema=[{"name": "id", "dataType": "INT"}],
            quality=[{"check": "row_count:\n  threshold:\n    must_be_greater_than: 0\n"}],
        )

        self.assertEqual(1, len(data_contract.schema_))
        self.assertEqual(1, len(data_contract.quality))
        self.assertIsNone(data_contract.quality[0].column)
