from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from statistics import median
from typing import Any

import psycopg  # type: ignore
from psycopg.types.json import Jsonb  # type: ignore

from genetic_lqo.core.types import ExecutionResult, PlanCacheConfig, hint_key
from genetic_lqo.execution.executor import PostgresConnectionConfig

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CACHE_SUFFIX = "_lqo_cache"


class PlanCacheUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedRuntime:
    median_seconds: float
    observation_count: int


@dataclass
class PlanCacheStats:
    runtime_hits: int = 0
    runtime_misses: int = 0
    stored_explain_count: int = 0
    stored_runtime_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class QueryCacheIdentity:
    query_hash: str
    normalized_sql: str


class PostgresPlanCache:
    def __init__(self, cache_connection: PostgresConnectionConfig, config: PlanCacheConfig,
                 planner_settings: dict[str, str] | None = None) -> None:
        self.cache_connection = cache_connection
        self.config = config
        self.planner_settings = dict(planner_settings or {})
        self.schema = _safe_schema(config.schema)
        self.warnings: list[str] = []
        self._stats = PlanCacheStats()
        self._conn: Any | None = None

    @classmethod
    def open(cls, *, business_connection: PostgresConnectionConfig, config: PlanCacheConfig,
             planner_settings: dict[str, str] | None = None) -> PostgresPlanCache:
        if not business_connection.dbname:
            raise PlanCacheUnavailable("plan cache requires a business database name")
        cache_connection = replace(business_connection,
                                   dbname=resolve_cache_dbname(business_connection.dbname, config))
        cache = cls(cache_connection, config, planner_settings)
        try:
            cache.initialize_schema()
        except psycopg.Error as exc:
            cache.close()
            raise PlanCacheUnavailable(f"plan cache {cache_connection.dbname!r} unavailable: {exc}") from exc
        return cache

    def initialize_schema(self) -> None:
        with self._connect().cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.explain (
                    query_hash text NOT NULL,
                    hints text NOT NULL,
                    query_name text NOT NULL DEFAULT '',
                    fingerprint text NOT NULL,
                    plan jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (query_hash, hints))""")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.runtime (
                    id bigserial PRIMARY KEY,
                    query_hash text NOT NULL,
                    fingerprint text NOT NULL,
                    query_name text NOT NULL DEFAULT '',
                    hints text NOT NULL DEFAULT '',
                    seconds double precision NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now())""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS runtime_lookup ON {self.schema}.runtime "
                        "(query_hash, fingerprint)")

    def store_explain(self, *, query_name: str, query_sql: str, hints: list[str] | tuple[str, ...],
                      result: ExecutionResult) -> None:
        if not result.success or not result.plan_digest:
            return
        query = query_cache_identity(query_sql, planner_settings=self.planner_settings)
        with self._connect().cursor() as cur:
            cur.execute(f"""
                INSERT INTO {self.schema}.explain (query_hash, hints, query_name, fingerprint, plan)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (query_hash, hints) DO UPDATE
                SET fingerprint = EXCLUDED.fingerprint, plan = EXCLUDED.plan, created_at = now()""",
                        (query.query_hash, _hints_text(hints), query_name, result.plan_digest,
                         Jsonb(result.explain_json)))
        self._stats.stored_explain_count += 1

    def lookup_runtime(self, query_sql: str, plan_digest: str,
                       hints: list[str] | tuple[str, ...] = ()) -> CachedRuntime | None:
        query = query_cache_identity(query_sql, planner_settings=self.planner_settings)
        with self._connect().cursor() as cur:
            cur.execute(f"SELECT seconds FROM {self.schema}.runtime WHERE query_hash = %s AND fingerprint = %s",
                        (query.query_hash, plan_digest))
            samples = [float(row[0]) for row in cur.fetchall()]
        if not samples:
            self._stats.runtime_misses += 1
            return None
        self._stats.runtime_hits += 1
        return CachedRuntime(median_seconds=float(median(samples)), observation_count=len(samples))

    def store_runtime(self, *, query_name: str, query_sql: str, hints: list[str] | tuple[str, ...],
                      result: ExecutionResult, individual_id: str = "") -> None:
        seconds = result.execution_time_seconds
        if (not result.success or not result.analyze or result.timed_out or not result.plan_digest
                or seconds is None or not math.isfinite(seconds) or seconds <= 0):
            return
        query = query_cache_identity(query_sql, planner_settings=self.planner_settings)
        with self._connect().cursor() as cur:
            cur.execute(f"INSERT INTO {self.schema}.runtime (query_hash, fingerprint, query_name, hints, seconds) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (query.query_hash, result.plan_digest, query_name, _hints_text(hints), seconds))
        self._stats.stored_runtime_count += 1

    def list_runtime_records(self, query_sql: str, *, limit: int = 100) -> list[dict[str, Any]]:
        query = query_cache_identity(query_sql, planner_settings=self.planner_settings)
        with self._connect().cursor() as cur:
            cur.execute(f"""
                SELECT fingerprint, count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds),
                       min(seconds), max(seconds), max(created_at), (array_agg(hints ORDER BY created_at DESC))[1]
                FROM {self.schema}.runtime WHERE query_hash = %s
                GROUP BY fingerprint ORDER BY max(created_at) DESC LIMIT %s""", (query.query_hash, limit))
            keys = ("plan_digest", "observations", "median_seconds", "min_seconds", "max_seconds",
                    "last_observed", "example_hints")
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def stats_snapshot(self) -> PlanCacheStats:
        return replace(self._stats)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connect(self) -> Any:
        if self._conn is None or self._conn.closed:
            cfg = self.cache_connection
            self._conn = psycopg.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname, user=cfg.user,
                                         password=cfg.password, autocommit=True)
        return self._conn


def resolve_cache_dbname(business_dbname: str, config: PlanCacheConfig) -> str:
    expected = f"{business_dbname}{CACHE_SUFFIX}"
    if config.dbname and config.dbname != expected:
        raise ValueError(f"Each business database requires its own plan cache: expected {expected!r}, "
                         f"got {config.dbname!r}")
    return expected


def query_cache_identity(sql: str, *, planner_settings: dict[str, str] | None = None) -> QueryCacheIdentity:
    normalized = normalize_sql_for_cache(sql)
    identity = normalized
    if planner_settings:
        identity = json.dumps([normalized, planner_settings], sort_keys=True, separators=(",", ":"))
    return QueryCacheIdentity(hashlib.sha256(identity.encode("utf-8")).hexdigest(), normalized)


def normalize_sql_for_cache(sql: str) -> str:
    return str(sql or "").strip().removesuffix(";").rstrip()


def create_cache_database(*, business_connection: PostgresConnectionConfig, config: PlanCacheConfig,
                          maintenance_dbname: str = "postgres") -> str:
    if not business_connection.dbname:
        raise ValueError("business database name is required")
    cache_dbname = resolve_cache_dbname(business_connection.dbname, config)
    cfg = business_connection
    conn = psycopg.connect(host=cfg.host, port=cfg.port, dbname=maintenance_dbname, user=cfg.user,
                           password=cfg.password, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cache_dbname,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {_quote_ident(cache_dbname)}")
    finally:
        conn.close()
    return cache_dbname


def _hints_text(hints: list[str] | tuple[str, ...]) -> str:
    return " ".join(hint_key(hints))


def _safe_schema(schema: str) -> str:
    if not _SAFE_IDENTIFIER_RE.match(schema):
        raise ValueError(f"unsafe plan cache schema name: {schema!r}")
    return _quote_ident(schema)


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
