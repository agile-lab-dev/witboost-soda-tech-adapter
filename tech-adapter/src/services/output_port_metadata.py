from __future__ import annotations

from typing import NamedTuple, Optional

from src.models.data_product_descriptor import OutputPort


class TableMetadata(NamedTuple):
    catalog: Optional[str]
    schema: Optional[str]
    table: Optional[str]


def resolve_output_port_table_metadata(output_port: OutputPort) -> TableMetadata:
    """
    Resolves the catalog/schema/table coordinates of the asset Soda must
    discover and monitor for a given Output Port.

    The Databricks Output Port template (with Soda DQ) can expose two
    distinct sets of table coordinates under `specific`:
      - `catalogName` / `schemaName` / `tableName`: the underlying storage
        table where the data physically lives.
      - `catalogNameOP` / `schemaNameOP` / `viewNameOP`: the actual Output
        Port asset (a view built over the storage table) exposed to
        consumers.

    Soda must target the Output Port's exposed asset (the `...OP` fields
    when present), since that is the consumable/governed object the data
    contract applies to -- the internal storage table may live in a schema
    that isn't even covered by the Soda Data Source's discovery scan (this
    caused `DATASET_NOT_FOUND` errors when the base fields were used
    instead). When the `...OP` fields are absent (e.g. simpler setups with
    no separate exposed view), the base fields are used as a fallback.
    """
    specific = output_port.specific
    catalog = specific.get("catalogNameOP") or specific.get("catalogName")
    schema = specific.get("schemaNameOP") or specific.get("schemaName")
    table = specific.get("viewNameOP") or specific.get("tableName")
    return TableMetadata(catalog=catalog, schema=schema, table=table)
