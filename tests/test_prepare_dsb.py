import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_dsb", ROOT / "scripts/prepare_dsb.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_existing_database_requires_matching_resume_identity():
    with pytest.raises(RuntimeError, match="existing database"):
        prepare.verify_database_identity(42, {}, False)
    with pytest.raises(RuntimeError, match="identity"):
        prepare.verify_database_identity(42, {"database_oid": 41}, True)
    with pytest.raises(RuntimeError, match="disappeared"):
        prepare.verify_database_identity(None, {"database_oid": 42}, True)
    prepare.verify_database_identity(42, {"database_oid": 42}, True)


def test_committed_table_cannot_be_reused_after_source_or_row_mutation():
    source = {"bytes": 12, "rows": 3, "sha256": "abc"}
    prepare.verify_loaded_table(source, 3, dict(source))
    with pytest.raises(RuntimeError, match="refusing to COPY again"):
        prepare.verify_loaded_table(source, 4, source)
    with pytest.raises(RuntimeError, match="refusing to COPY again"):
        prepare.verify_loaded_table(source, 3, {**source, "sha256": "changed"})


def test_authoritative_schema_defers_every_primary_key_without_losing_columns():
    text = (ROOT / "genetic_lqo/workload/dsb/schema.sql").read_text()
    tables, primary_keys = prepare.schema_steps(text)
    assert len(tables) == 25
    assert len(primary_keys) == 24
    assert all("primary key" not in statement.lower() for statement in tables.values())
    assert "decimal(7,2)" in tables["store_sales"]
    assert "PRIMARY KEY (ss_item_sk, ss_ticket_number)" in primary_keys["store_sales_pkey"]
