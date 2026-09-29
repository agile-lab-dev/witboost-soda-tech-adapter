from pathlib import Path

import yaml

from src.models.data_product_descriptor import DataProduct
from src.services.validate_service import validate_soda_workload

VALID_DESCRIPTOR_STR = Path("tests/descriptors/descriptor_soda_workload_valid.yaml").read_text()
SODA_COMPONENT_ID = "urn:dmb:cmp:finance:orders:0:orders-output-port:soda-workload"
OUTPUT_PORT_ID = "urn:dmb:cmp:finance:orders:0:orders-output-port"


def _data_product_from(descriptor_str: str) -> DataProduct:
    raw = yaml.safe_load(descriptor_str)
    return DataProduct(**raw["dataProduct"])


def test_valid_soda_workload_passes():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is True
    assert result.error is None


def test_component_not_found():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)

    result = validate_soda_workload(data_product, "urn:dmb:cmp:finance:orders:0:does-not-exist")

    assert result.valid is False
    assert "not found in the Data Product descriptor" in result.error.errors[0]


def test_component_not_a_soda_workload():
    data_product = _data_product_from(VALID_DESCRIPTOR_STR)

    result = validate_soda_workload(data_product, OUTPUT_PORT_ID)

    assert result.valid is False
    assert "is not a Soda workload" in result.error.errors[0]


def test_empty_data_source_name():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "dataSourceName: databricks-production", "dataSourceName: ''"
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any("`dataSourceName` must be a non-empty string." in e for e in result.error.errors)


def test_empty_output_port_ids():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "              outputPortIds:\n                - urn:dmb:cmp:finance:orders:0:orders-output-port\n",
        "              outputPortIds: []\n",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any(
        "`outputPortIds` must contain at least one Output Port id." in e for e in result.error.errors
    )


def test_output_port_not_found():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "- urn:dmb:cmp:finance:orders:0:orders-output-port",
        "- urn:dmb:cmp:finance:orders:0:missing-output-port",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any("was not found in the Data Product descriptor" in e for e in result.error.errors)


def test_missing_table_metadata():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "        catalogName: main\n        schemaName: sales\n        tableName: orders\n",
        "        catalogName: main\n",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any("missing required table metadata" in e for e in result.error.errors)


def test_missing_quality_checks():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "        quality:\n"
        "          - check: |\n"
        "              row_count:\n"
        "                threshold:\n"
        "                  must_be_greater_than: 0\n"
        "          - check: |\n"
        "              missing:\n"
        "            column: transaction_id\n",
        "        quality: []\n",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any("has no data quality checks defined" in e for e in result.error.errors)


def test_check_not_valid_yaml():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "          - check: |\n"
        "              row_count:\n"
        "                threshold:\n"
        "                  must_be_greater_than: 0\n",
        "          - check: \"row_count: [\"\n",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any("is not valid YAML" in e for e in result.error.errors)


def test_check_not_a_single_key_mapping():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "          - check: |\n"
        "              row_count:\n"
        "                threshold:\n"
        "                  must_be_greater_than: 0\n",
        "          - check: \"row_count: {}\\nfreshness: {}\"\n",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any("must be a YAML mapping with exactly one check-type key" in e for e in result.error.errors)


def test_unrecognized_check_type():
    descriptor_str = VALID_DESCRIPTOR_STR.replace(
        "          - check: |\n"
        "              row_count:\n"
        "                threshold:\n"
        "                  must_be_greater_than: 0\n",
        "          - check: \"hello: world\"\n",
    )
    data_product = _data_product_from(descriptor_str)

    result = validate_soda_workload(data_product, SODA_COMPONENT_ID)

    assert result.valid is False
    assert any(
        "is not a recognized Soda Contract Language check type" in e for e in result.error.errors
    )
