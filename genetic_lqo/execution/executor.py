from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import psycopg  # type: ignore

from genetic_lqo.core.types import DatabaseConfig, ExecutionResult, HintDiagnostics
from genetic_lqo.planning.dialect import (
    build_sql_with_hints,
    deduplicate_hints,
    hint_diagnostic_members_match,
    normalize_hint,
)
from genetic_lqo.planning.plan import build_plan_summary, plan_digest

_HINT_DEBUG_SETTINGS = (
    ("client_min_messages", "notice"),
    ("pg_hint_plan.debug_print", "on"),
    ("pg_hint_plan.message_level", "notice"),
    ("pg_hint_plan.parse_messages", "notice"),
)


@dataclass(frozen=True)
class PostgresConnectionConfig:
    host: str | None = None
    port: int | None = None
    dbname: str | None = None
    user: str | None = None
    password: str | None = None

    @staticmethod
    def from_env(dbname: str | None = None) -> PostgresConnectionConfig:
        port_raw = os.getenv("DB_PORT")
        return PostgresConnectionConfig(
            host=os.getenv("DB_HOST"),
            port=int(port_raw) if port_raw else None,
            dbname=dbname or os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
        )

    @staticmethod
    def from_config(config: DatabaseConfig, dbname: str | None = None) -> PostgresConnectionConfig:
        return PostgresConnectionConfig(
            host=config.host or None,
            port=config.port,
            dbname=dbname or config.dbname or None,
            user=config.user or None,
            password=config.password or None,
        )


@dataclass(frozen=True)
class IndexMetadata:
    table_name: str
    index_name: str
    index_def: str
    access_method: str = ""


class PostgresGeneticExecutor:
    def __init__(
        self,
        connection_config: PostgresConnectionConfig,
        *,
        planner_settings: Mapping[str, str] | None = None,
    ) -> None:
        self.connection_config = connection_config
        self.planner_settings = MappingProxyType(dict(planner_settings or {}))

    def explain(
        self,
        sql: str,
        hints: list[str] | tuple[str, ...] = (),
        *,
        analyze: bool = False,
        timeout_seconds: float | None = None,
        timing: bool = True,
        buffers: bool = True,
    ) -> ExecutionResult:
        hinted_sql = build_sql_with_hints(sql, hints)
        options = ["FORMAT JSON", "VERBOSE", "SETTINGS"]
        if analyze:
            options.insert(0, "ANALYZE")
            if buffers:
                options.insert(1, "BUFFERS")
            if not timing:
                options.insert(1, "TIMING OFF")
        statement = f"EXPLAIN ({', '.join(options)})\n{hinted_sql}"
        started = time.monotonic()
        notices: list[str] = []
        conn = None
        try:
            conn = self._connect()
            if hints:
                conn.add_notice_handler(lambda d: notices.append(
                    "\n".join(part for part in (str(d.message_primary or ""), str(d.message_detail or "")) if part)))
            with conn.cursor() as cur:
                if hints:
                    for name, value in _HINT_DEBUG_SETTINGS:
                        cur.execute("SELECT set_config(%s, %s, true)", (name, value))
                if timeout_seconds is not None and timeout_seconds > 0:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                                (f"{max(1, int(timeout_seconds * 1000))}ms",))
                cur.execute(statement)
                row = cur.fetchone()
            conn.rollback()
            payload = row[0] if row else None
            digest = plan_digest(payload)
            summary = build_plan_summary(payload, fallback_digest=digest)
            return ExecutionResult(
                sql=hinted_sql, hints=tuple(hints), success=True, analyze=analyze,
                plan_digest=digest, explain_json=payload, plan_summary=summary,
                hint_diagnostics=parse_hint_diagnostics(hints, notices),
                execution_time_seconds=summary.execution_time_seconds,
                planning_time_seconds=summary.planning_time_seconds,
                elapsed_wall_seconds=time.monotonic() - started, timeout_seconds=timeout_seconds,
            )
        except psycopg.errors.QueryCanceled as exc:
            return ExecutionResult(
                sql=hinted_sql, hints=tuple(hints), success=False, analyze=analyze, timed_out=True,
                timeout_seconds=timeout_seconds, error=str(exc), hint_diagnostics=parse_hint_diagnostics(hints, notices),
                elapsed_wall_seconds=time.monotonic() - started,
            )
        except psycopg.Error as exc:
            return ExecutionResult(
                sql=hinted_sql, hints=tuple(hints), success=False, analyze=analyze,
                error=str(exc), hint_diagnostics=parse_hint_diagnostics(hints, notices),
                elapsed_wall_seconds=time.monotonic() - started,
            )
        finally:
            if conn is not None:
                conn.close()

    def fetch_indexes(self, table_names: list[str]) -> dict[str, list[IndexMetadata]]:
        if not table_names:
            return {}
        query = """
            SELECT
                tbl.relname AS table_name,
                idx.relname AS index_name,
                am.amname AS access_method,
                pg_get_indexdef(idx.oid) AS index_def
            FROM pg_class AS tbl
            JOIN pg_index AS ix ON ix.indrelid = tbl.oid
            JOIN pg_class AS idx ON idx.oid = ix.indexrelid
            JOIN pg_am AS am ON am.oid = idx.relam
            WHERE tbl.relkind IN ('r', 'p')
              AND ix.indisvalid
              AND ix.indisready
              AND tbl.relname = ANY(%s)
            ORDER BY tbl.relname, idx.relname
        """
        out: dict[str, list[IndexMetadata]] = {}
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, (table_names,))
                for table_name, index_name, access_method, index_def in cur.fetchall():
                    out.setdefault(str(table_name), []).append(
                        IndexMetadata(
                            table_name=str(table_name),
                            index_name=str(index_name),
                            access_method=str(access_method),
                            index_def=str(index_def),
                        )
                    )
        finally:
            conn.close()
        return out

    def fetch_table_statistics(self, alias_map: dict[str, str]) -> list[dict[str, Any]]:
        entries = []
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('statement_timeout', '10000', true)")
                for ordinal, (alias, relation) in enumerate(sorted(alias_map.items())):
                    cur.execute("""
                        SELECT c.oid, n.nspname, c.relname, c.reltuples,
                               pg_total_relation_size(c.oid), c.relpages
                        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE c.oid=to_regclass(%s)
                    """, (relation,))
                    table = cur.fetchone()
                    if table is None:
                        raise ValueError(f"Statistics relation not found: {relation}")
                    oid, schema, name, tuples, size, pages = table
                    n_rows = float(tuples) if tuples >= 0 else None
                    cur.execute("""
                        SELECT a.attname, format_type(a.atttypid,a.atttypmod), NOT a.attnotnull,
                               s.null_frac, s.avg_width, s.n_distinct, s.correlation,
                               to_jsonb(s.most_common_vals), to_jsonb(s.most_common_freqs),
                               to_jsonb(s.histogram_bounds)
                        FROM pg_attribute a
                        LEFT JOIN pg_stats s ON s.schemaname=%s AND s.tablename=%s
                            AND s.attname=a.attname AND NOT s.inherited
                        WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
                        ORDER BY a.attnum
                    """, (schema, name, oid))
                    columns, stats = [], []
                    for column, dtype, nullable, null_frac, width, distinct, correlation, mcv, freqs, histogram in cur.fetchall():
                        columns.append([column, dtype, nullable])
                        resolved_distinct = (float(distinct) if distinct is not None and distinct >= 0
                                             else -float(distinct) * n_rows
                                             if distinct is not None and n_rows is not None else None)
                        stats.append({"column": column, "available": null_frac is not None,
                                      "null_fraction": null_frac, "average_width": width,
                                      "n_distinct_raw": distinct, "estimated_distinct_values": resolved_distinct,
                                      "correlation": correlation, "most_common_values": mcv or [],
                                      "most_common_frequencies": freqs or [], "histogram_bounds": histogram or []})
                    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s ORDER BY oid", (oid,))
                    constraints = [row[0] for row in cur.fetchall()]
                    entries.append({"id": ordinal, "node": {"postgresScan": {
                        "alias": alias, "relation": f"{schema}.{name}", "baseConf": {
                            "schema": {"columns": columns}, "constraints": constraints,
                            "statistics": {"n_rows": n_rows, "total_bytes": int(size), "pages": pages,
                                           "n_rows_kind": "pg_class.reltuples_estimate",
                                           "columnStats": stats}},
                        "provenance": {"source": "pg_class/pg_attribute/pg_stats",
                                       "column_statistics_kind": "ANALYZE_sample_estimates"}}}})
            conn.rollback()
        finally:
            conn.close()
        return entries

    def verify_pg_hint_plan(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('shared_preload_libraries'), "
                            "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_hint_plan'), "
                            "current_setting('server_version')")
                preload, installed, version = cur.fetchone()
        finally:
            conn.close()
        preloaded = "pg_hint_plan" in {item.strip() for item in str(preload).split(",")}
        return {"server_version": version, "pg_hint_plan_preloaded": preloaded,
                "pg_hint_plan_installed": bool(installed), "pg_hint_plan_loaded": preloaded and bool(installed)}

    def _connect(self):
        cfg = self.connection_config
        conn = psycopg.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname, user=cfg.user, password=cfg.password)
        try:
            with conn.cursor() as cur:
                for name, value in self.planner_settings.items():
                    cur.execute("SELECT set_config(%s, %s, false)", (name, value))
        except BaseException:
            conn.close()
            raise
        return conn


def parse_hint_diagnostics(
    requested_hints: list[str] | tuple[str, ...],
    messages: list[str] | tuple[str, ...],
) -> HintDiagnostics:
    requested = tuple(deduplicate_hints(requested_hints))
    if not requested:
        return HintDiagnostics(status="not_requested", messages=tuple(messages))

    sections: dict[str, list[str]] = {
        "used hint:": [],
        "not used hint:": [],
        "duplication hint:": [],
        "error hint:": [],
    }
    current_section = ""
    diagnostic_block_seen = False
    for message in messages:
        for raw_line in str(message).splitlines():
            line = raw_line.strip()
            lowered = line.lower()
            if lowered == "pg_hint_plan:":
                diagnostic_block_seen = True
                current_section = ""
                continue
            if lowered in sections:
                diagnostic_block_seen = True
                current_section = lowered
                continue
            if diagnostic_block_seen and current_section and line:
                sections[current_section].append(normalize_hint(line))

    used = tuple(sections["used hint:"])
    not_used = tuple(sections["not used hint:"])
    duplicate = tuple(sections["duplication hint:"])
    errors = tuple(sections["error hint:"])
    if errors:
        status = "error"
    elif duplicate:
        status = "duplicate"
    elif not_used:
        status = "not_used"
    elif diagnostic_block_seen and hint_diagnostic_members_match(used, requested):
        status = "all_used"
    elif diagnostic_block_seen:
        status = "incomplete"
    else:
        status = "missing"
    return HintDiagnostics(
        status=status,
        requested_hints=requested,
        used_hints=used,
        not_used_hints=not_used,
        duplicate_hints=duplicate,
        error_hints=errors,
        messages=tuple(messages),
    )
