import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, List, Literal, Optional, Type

import yaml
from loguru import logger
from pydantic import (
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.models.constants import OPENMETADATA_SUPPORTED_DATATYPES


class ComponentKind(StrEnum):
    OUTPUTPORT = "outputport"
    WORKLOAD = "workload"
    STORAGE = "storage"
    OBSERVABILITY = "observability"


class CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value):
        value = value.lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class TagSourceTagLabel(CaseInsensitiveEnum):
    CLASSIFICATION = "Classification"
    GLOSSARY = "Glossary"


class LabelTypeTagLabel(CaseInsensitiveEnum):
    MANUAL = "Manual"
    PROPAGATED = "Propagated"
    AUTOMATED = "Automated"
    DERIVED = "Derived"


class StateTagLabel(CaseInsensitiveEnum):
    SUGGESTED = "Suggested"
    CONFIRMED = "Confirmed"


class OpenMetadataTagLabel(BaseModel):
    tagFQN: str
    description: Optional[str] = None
    source: TagSourceTagLabel = TagSourceTagLabel.CLASSIFICATION
    labelType: LabelTypeTagLabel = LabelTypeTagLabel.MANUAL
    state: StateTagLabel = StateTagLabel.CONFIRMED
    href: Optional[str] = None


class ConnectionTypeWorkload(CaseInsensitiveEnum):
    HOUSEKEEPING = "HOUSEKEEPING"
    DATAPIPELINE = "DATAPIPELINE"


class OpenMetadataColumn(BaseModel):
    name: str
    dataType: str
    dataLength: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    description: Optional[str] = None
    tags: Optional[List[OpenMetadataTagLabel]] = None

    @field_validator("dataType")
    @classmethod
    def check_dataType(cls, value, values):
        if value.upper() not in OPENMETADATA_SUPPORTED_DATATYPES:
            raise ValueError(
                'Column "'
                + values["name"]
                + '" specifies dataType of "'
                + value
                + '" but this is not a valid OpenMetadata data type'
            )
        return value


class QualityCheck(BaseModel):
    """
    A single Soda data quality check, expressed as a raw Soda Contract
    Language YAML fragment (a single check-type mapping), e.g.:
    `check: "row_count:\n  threshold:\n    must_be_greater_than: 0\n"`.
    See https://docs.soda.io/reference/contract-language-reference.

    If `column` is set, the check is placed under that column's `checks:`
    list in the generated contract (column-level check, e.g. `missing`,
    `invalid`, single-column `duplicate`). If `column` is omitted, the check
    is placed under the dataset-level `checks:` list (e.g. `schema`,
    `row_count`, `freshness`, `metric`, `failed_rows`, `group_by`).
    """

    check: str
    column: Optional[str] = None


def _normalize_physical_type(physical_type: Any) -> dict:
    """
    Convert an ODCS v3.2 `physicalType` (e.g. `VARCHAR(100)`, `DECIMAL(10,2)`)
    into the legacy `{dataType, dataLength, precision, scale}` fields expected
    by `OpenMetadataColumn`. Falls back to passing the value through as-is
    when it doesn't match a known parametrized pattern.
    """
    if not isinstance(physical_type, str):
        return {"dataType": physical_type}

    value = physical_type.strip()

    length_match = re.fullmatch(r"(VARCHAR|CHAR)\s*\(\s*(\d+)\s*\)", value, flags=re.IGNORECASE)
    if length_match:
        return {"dataType": length_match.group(1).upper(), "dataLength": int(length_match.group(2))}

    decimal_match = re.fullmatch(
        r"(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)",
        value,
        flags=re.IGNORECASE,
    )
    if decimal_match:
        return {
            "dataType": decimal_match.group(1).upper(),
            "precision": int(decimal_match.group(2)),
            "scale": int(decimal_match.group(3)),
        }

    return {"dataType": value.upper()}


def _normalize_column_tags(tags: Any) -> list:
    """
    Convert ODCS v3.2 `properties[].tags` entries into the
    `OpenMetadataTagLabel`-compatible shape expected by `OpenMetadataColumn`.

    ODCS allows tags to be plain strings (e.g. `tags: [bt1, bt2]`), whereas
    the legacy model requires a `{tagFQN, ...}` object. Plain strings are
    wrapped as `{"tagFQN": <value>}`; dicts (already in the legacy shape)
    are passed through unchanged.
    """
    if not isinstance(tags, list):
        return []

    normalized = []
    for tag in tags:
        if isinstance(tag, str):
            normalized.append({"tagFQN": tag})
        elif isinstance(tag, dict):
            normalized.append(tag)
    return normalized


def _flatten_odcs_quality_item(item: dict) -> dict:
    """
    Convert one ODCS v3.2 `dataContract.schema[].quality[]` entry
    (`{id, type, engine, implementation}`) into the adapter's legacy
    `{check, column}` shape.

    `implementation` is a raw Soda Contract Language YAML fragment. Per
    the current output-port template contract, when the check is
    column-scoped, `implementation` contains an extra top-level `column`
    key (a sibling of the check-type key) inside the same YAML fragment;
    that key is extracted as the column name and stripped out before the
    remainder is treated as the Soda check body. When no `column` key is
    present, the check is dataset-level.
    """
    implementation = item.get("implementation") or ""
    check_str = implementation
    column = None

    try:
        parsed = yaml.safe_load(implementation)
    except yaml.YAMLError:
        parsed = None

    if isinstance(parsed, dict) and "column" in parsed:
        parsed = dict(parsed)
        column = parsed.pop("column")
        check_str = yaml.safe_dump(parsed, sort_keys=False)

    return {"check": check_str, "column": column}


class DataContract(BaseModel):
    schema_: Optional[List[OpenMetadataColumn]] = Field(default=None, alias="schema")
    quality: Optional[List[QualityCheck]] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_odcs_v32(cls, value: Any) -> Any:
        """
        Accept both the legacy flat shape (`schema`: list of columns,
        `quality`: list of `{check, column}`) and the ODCS v3.2 shape
        emitted by the current output-port templates (`schema`: list of
        tables, each with `properties` (columns) and its own `quality`
        list of `{id, type, engine, implementation}`).

        Flattens the ODCS shape into the legacy fields so the rest of the
        adapter (validation, contract building) keeps consuming
        `dataContract.schema_` and `dataContract.quality` unchanged.
        """
        if not isinstance(value, dict):
            return value

        schema_value = value.get("schema")
        if not isinstance(schema_value, list) or not schema_value:
            return value

        is_odcs_v32 = all(
            isinstance(entry, dict) and ("properties" in entry or "quality" in entry) for entry in schema_value
        )
        if not is_odcs_v32:
            return value

        flattened_columns: list[dict] = []
        flattened_quality: list[dict] = []
        for table in schema_value:
            for prop in table.get("properties") or []:
                if not isinstance(prop, dict):
                    continue
                physical_type = prop.get("physicalType") or prop.get("logicalType")
                flattened_columns.append(
                    {
                        "name": prop.get("name"),
                        **_normalize_physical_type(physical_type),
                        "description": prop.get("description"),
                        "tags": _normalize_column_tags(prop.get("tags")),
                    }
                )
            for quality_item in table.get("quality") or []:
                if isinstance(quality_item, dict):
                    flattened_quality.append(_flatten_odcs_quality_item(quality_item))

        result = {**value, "schema": flattened_columns}
        if flattened_quality:
            result["quality"] = flattened_quality
        return result


class DataSharingAgreement(BaseModel):
    purpose: Optional[str] = None
    billing: Optional[str] = None
    security: Optional[str] = None
    intendedUsage: Optional[str] = None
    limitations: Optional[str] = None
    lifeCycle: Optional[str] = None
    confidentiality: Optional[str] = None


class Component(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: ComponentKind

    id: str
    name: str
    fullyQualifiedName: Optional[str] = None
    description: str
    specific: dict


class OutputPort(Component):
    model_config = ConfigDict(extra="allow")
    kind: Literal[ComponentKind.OUTPUTPORT]

    version: str
    infrastructureTemplateId: str
    useCaseTemplateId: Optional[str] = None
    dependsOn: List[str]
    platform: Optional[str] = None
    technology: Optional[str] = None
    outputPortType: str
    creationDate: Optional[datetime] = None
    startDate: Optional[datetime] = None
    retentionTime: Optional[str] = None
    processDescription: Optional[str] = None
    dataContract: DataContract
    dataSharingAgreement: Optional[DataSharingAgreement] = None
    tags: List[OpenMetadataTagLabel]
    sampleData: Optional[dict] = None  # OpenMetadataTable
    semanticLinking: List[dict]

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value, values):
        if value == ComponentKind.OUTPUTPORT:
            return value
        else:
            raise ValueError(
                f"kind of component with id {values.get('id')} must be 'outputport'"  # noqa: E501
            )


class Workload(Component):
    model_config = ConfigDict(extra="allow")
    kind: Literal[ComponentKind.WORKLOAD]

    version: str
    infrastructureTemplateId: str
    useCaseTemplateId: Optional[str] = None
    dependsOn: List[str]
    platform: Optional[str] = None
    technology: Optional[str] = None
    workloadType: Optional[str] = None
    connectionType: ConnectionTypeWorkload
    tags: List[OpenMetadataTagLabel]
    readsFrom: Optional[List[str]] = None

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value, values):
        if value == ComponentKind.WORKLOAD:
            return value
        else:
            raise ValueError(
                f"kind of component with id {values.get('id')} must be 'workload'"  # noqa: E501
            )


class StorageArea(Component):
    model_config = ConfigDict(extra="allow")
    kind: Literal[ComponentKind.STORAGE]

    infrastructureTemplateId: str
    useCaseTemplateId: Optional[str] = None
    dependsOn: List[str]
    platform: Optional[str] = None
    technology: Optional[str] = None
    storageType: Optional[str] = None
    tags: List[OpenMetadataTagLabel]

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value, values):
        if value == ComponentKind.STORAGE:
            return value
        else:
            raise ValueError(
                f"kind of component with id {values.get('id')} must be 'storage'"  # noqa: E501
            )


class Observability(Component):
    model_config = ConfigDict(extra="allow")
    kind: Literal[ComponentKind.OBSERVABILITY]

    endpoint: AnyUrl
    completeness: dict
    dataProfiling: dict
    freshness: dict
    availability: dict
    dataQuality: dict

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value, values):
        if value == ComponentKind.OBSERVABILITY:
            return value
        else:
            raise ValueError(
                f"kind of component with id {values.get('id')} must be 'observability'"  # noqa: E501
            )


component_map: dict[str, Type[Component]] = {
    ComponentKind.OBSERVABILITY: Observability,
    ComponentKind.OUTPUTPORT: OutputPort,
    ComponentKind.WORKLOAD: Workload,
    ComponentKind.STORAGE: StorageArea,
}


def parse_component(data: dict | Component) -> Component:
    if isinstance(data, Component):  # happens if DP is created in code
        if data.kind not in component_map:
            raise ValueError(f"Unknown component kind: {data.kind}")
        return data
    else:
        kind = data.get("kind")
        if kind not in component_map:
            raise ValueError(f"Unknown component kind: {kind}")
        component = component_map[kind](**data)
        logger.debug("Parsed component: " + str(component))
        return component


class DataProduct(BaseModel):
    id: str
    name: str
    fullyQualifiedName: Optional[str] = None
    description: str
    kind: Literal["dataproduct"]
    domain: str
    version: str
    environment: str
    dataProductOwner: str
    dataProductOwnerDisplayName: Optional[str] = None
    email: Optional[str] = None
    ownerGroup: str
    devGroup: str
    informationSLA: Optional[str] = None
    status: Optional[str] = None
    maturity: Optional[str] = None
    billing: Optional[dict] = None
    tags: List[OpenMetadataTagLabel]
    specific: dict
    components: List[Annotated[Component, BeforeValidator(parse_component)]]

    def get_components_by_kind(self, kind: str) -> List[Component]:
        """
        Filters the components associated with the data product and returns
        a list containing only the components that have the specified kind.

        Args:
            kind (str): The kind of components to retrieve.

        Returns:
            List[Component]: A list of Component objects that match the specified kind.
            If no matching components are found, an empty list is returned.

        Example:
            To retrieve all components of kind 'outputport' from a data product 'my_data_product':
            >>> outputport_components = my_data_product.get_components_by_kind('outputport')
        """  # noqa: E501

        new_components_list = [component for component in self.components if component.kind == kind]

        return new_components_list

    def get_component_by_id(self, component_id: str) -> Component | None:
        """
        Retrieve a component within the data product by its unique identifier.

        This method searches for a component with the specified ID within the data product's
        list of components and returns the matching component, if found.

        Args:
            component_id (str): The unique identifier of the component to retrieve.

        Returns:
            Component | None: The Component object with the specified ID if found, or None if
            no matching component is found.

        Searches recursively into each component's nested 'components' list, if
        present, so subcomponents (e.g. a Soda workload nested under an output
        port, or a guardian sub-component nested under a workload) are
        resolvable by their own id, not just top-level components.

        Example:
           To retrieve a specific component with ID '12345' from a data product 'my_data_product':
           >>> specific_component = my_data_product.get_component_by_id('12345')
           >>> if specific_component:
           ...     print(f"Found component: {specific_component.name}")
           ... else:
           ...     print("Component not found.")
        """  # noqa: E501
        return self._find_component(self.components, component_id)

    @staticmethod
    def _find_component(components: List[Component], component_id: str) -> Component | None:
        for component in components:
            if component.id == component_id:
                return component
            nested_raw = (component.model_extra or {}).get("components")
            if nested_raw:
                nested = [parse_component(item) for item in nested_raw]
                found = DataProduct._find_component(nested, component_id)
                if found is not None:
                    return found
        return None

    def get_typed_component_by_id(self, component_id: str, component_type: Type[BaseModel]):
        component = self.get_component_by_id(component_id)
        if component is not None:
            return component_type.model_validate(component.model_dump(by_alias=True))
        else:
            return None

    def get_output_ports(self) -> List[OutputPort]:
        """
        Retrieve a list of output ports associated with the data product.

        This method filters the components of kind 'outputport' that are associated with
        the data product and returns a list containing these output ports.

        Returns:
            List[OutputPort]: A list of OutputPort objects that represent the output
            ports associated with the data product.

        Example:
            To retrieve all output ports from a data product 'my_data_product':
            >>> output_ports = my_data_product.get_output_ports()
        """  # noqa: E501

        output_ports: List[OutputPort] = []
        for op in self.get_components_by_kind("outputport"):
            if type(op) is OutputPort:
                output_ports.append(op)
        return output_ports

    def get_workloads(self) -> List[Workload]:
        """
        Retrieve a list of workloads associated with the data product.

        This method filters the components of kind 'workload' that are associated with
        the data product and returns a list containing these workloads.

        Returns:
            List[Workload]: A list of Workload objects that represent the workloads
            associated with the data product.

        Example:
            To retrieve all workloads from a data product 'my_data_product':
            >>> workloads = my_data_product.get_workloads()
        """  # noqa: E501
        workloads: List[Workload] = []
        for wl in self.get_components_by_kind("workload"):
            if type(wl) is Workload:
                workloads.append(wl)
        return workloads

    def get_storage_areas(self) -> List[StorageArea]:
        """
        Retrieve a list of storage areas associated with the data product.

        This method filters the components of kind 'storage' that are associated with
        the data product and returns a list containing these storage areas.

        Returns:
            List[StorageArea]: A list of StorageArea objects that represent the storage
            areas associated with the data product.

        Example:
            To retrieve all storage areas from a data product 'my_data_product':
            >>> storage_areas = my_data_product.get_storage_areas()
        """  # noqa: E501
        storage_areas: List[StorageArea] = []
        for st in self.get_components_by_kind("storage"):
            if type(st) is StorageArea:
                storage_areas.append(st)
        return storage_areas

    def get_observability_APIs(self) -> List[Observability]:
        """
        Retrieve a list of observability APIs associated with the data product.

        This method filters the components of kind 'observability' that are associated with
        the data product and returns a list containing these observability APIs.

        Returns:
            List[Observability]: A list of Observability objects that represent the observability
            APIs associated with the data product.

        Example:
            To retrieve all observability APIs from a data product 'my_data_product':
            >>> observability_apis = my_data_product.get_observability_APIs()
        """  # noqa: E501
        observability_APIs: List[Observability] = []
        for obs in self.get_components_by_kind("observability"):
            if type(obs) is Observability:
                observability_APIs.append(obs)
        return observability_APIs
