from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import sqlglot  # type: ignore
from sqlglot.optimizer.scope import Scope, build_scope  # type: ignore

from genetic_lqo.core.artifacts import canonical_json_sha256
from genetic_lqo.core.types import EstimationError, JoinMethodObservation, PlanSummary, RuntimeHotspot

_SCAN_NODE_MARKERS = ("Scan",)
_JOIN_NODE_TYPES = {"Nested Loop", "Hash Join", "Merge Join"}
_ESTIMATION_ERROR_THRESHOLD = 10.0
QUERY_STRUCTURE_RECEIPT_SCHEMA_VERSION = 3
ROOT_QUERY_SCOPE_POLICY = "root_select_direct_single_table_correlated_exists_v1"
SQLGLOT_DIALECT = "postgres"


class SchemaResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ColumnResolutionReceipt:
    mode: str
    hintable_scope: str
    scope_policy: str
    parser_name: str
    parser_version: str
    dialect: str
    query_block_count: int
    excluded_query_block_count: int
    relation_occurrence_count: int
    non_hintable_relation_occurrence_count: int
    correlated_column_count: int
    total_resolved_column_count: int
    schema_catalog_sha256: str
    alias_count: int
    qualified_column_count: int
    unqualified_column_count: int
    resolved_column_count: int


@dataclass(frozen=True)
class NonHintableQueryBlock:
    scope_id: str
    parent_scope_id: str
    kind: str
    normalized_sql_sha256: str
    alias_map: dict[str, str]
    referenced_columns_by_alias: dict[str, frozenset[str]]
    outer_referenced_columns_by_alias: dict[str, frozenset[str]]
    join_edges: frozenset[tuple[str, str]]
    correlation_edges: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class PredicateCatalogEntry:

    predicate_id: str
    normalized_sql: str
    referenced_aliases: tuple[str, ...]
    volatility_class: str
    join_semantics: str
    eligible_for_pushdown: bool


@dataclass(frozen=True)
class QueryStructure:
    query_sql_sha256: str
    alias_map: dict[str, str]
    join_edges: frozenset[tuple[str, str]]
    referenced_columns_by_alias: dict[str, frozenset[str]]
    non_hintable_query_blocks: tuple[NonHintableQueryBlock, ...]
    resolution_receipt: ColumnResolutionReceipt
    predicate_catalog: tuple[PredicateCatalogEntry, ...] = ()
    exact_cardinality_supported: bool = False
    exact_cardinality_reason_codes: tuple[str, ...] = ()

    @property
    def non_hintable_aliases(self) -> frozenset[str]:
        return frozenset(
            alias
            for block in self.non_hintable_query_blocks
            for alias in block.alias_map
        )


def query_structure_receipt_payload(structure: QueryStructure) -> dict[str, Any]:

    payload: dict[str, Any] = {
        "schema_version": QUERY_STRUCTURE_RECEIPT_SCHEMA_VERSION,
        "query_sql_sha256": structure.query_sql_sha256,
        "alias_map": structure.alias_map,
        "join_edges": [list(edge) for edge in sorted(structure.join_edges)],
        "referenced_columns_by_alias": {
            alias: sorted(columns)
            for alias, columns in sorted(structure.referenced_columns_by_alias.items())
        },
        "non_hintable_aliases": sorted(structure.non_hintable_aliases),
        "predicate_catalog": [asdict(item) for item in structure.predicate_catalog],
        "exact_cardinality_supported": structure.exact_cardinality_supported,
        "exact_cardinality_reason_codes": list(structure.exact_cardinality_reason_codes),
        "non_hintable_query_blocks": [
            {
                "scope_id": block.scope_id,
                "parent_scope_id": block.parent_scope_id,
                "kind": block.kind,
                "normalized_sql_sha256": block.normalized_sql_sha256,
                "alias_map": block.alias_map,
                "referenced_columns_by_alias": {
                    alias: sorted(columns)
                    for alias, columns in sorted(block.referenced_columns_by_alias.items())
                },
                "outer_referenced_columns_by_alias": {
                    alias: sorted(columns)
                    for alias, columns in sorted(block.outer_referenced_columns_by_alias.items())
                },
                "join_edges": [list(edge) for edge in sorted(block.join_edges)],
                "correlation_edges": [list(edge) for edge in sorted(block.correlation_edges)],
            }
            for block in structure.non_hintable_query_blocks
        ],
        "resolution_receipt": asdict(structure.resolution_receipt),
    }
    payload["payload_sha256"] = canonical_json_sha256(payload)
    return payload


def extract_alias_map(sql: str) -> dict[str, str]:

    try:
        expr = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return {}

    root_scope = _root_scope(expr, strict=False)
    return _alias_map_from_scope(root_scope, strict=False) if root_scope is not None else {}


def extract_join_edges(sql: str, aliases: set[str] | None = None) -> frozenset[tuple[str, str]]:

    try:
        expr = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return frozenset()
    root_scope = _root_scope(expr, strict=False)
    if root_scope is None:
        return frozenset()
    root_columns = {id(column) for column in root_scope.columns}
    comparison_types = (
        sqlglot.expressions.EQ,
        sqlglot.expressions.NEQ,
        sqlglot.expressions.GT,
        sqlglot.expressions.GTE,
        sqlglot.expressions.LT,
        sqlglot.expressions.LTE,
    )
    edges: set[tuple[str, str]] = set()
    for predicate in root_scope.find_all(*comparison_types):
        referenced = set()
        for column in predicate.find_all(sqlglot.expressions.Column):
            if id(column) not in root_columns:
                continue
            qualifier = postgres_identifier_name(column.args.get("table"))
            if qualifier and (aliases is None or qualifier in aliases):
                referenced.add(qualifier)
        for left, right in itertools.combinations(sorted(referenced), 2):
            edges.add((left, right))
    return frozenset(edges)


def parse_schema_relation_columns(schema_sql: str) -> dict[str, frozenset[str]]:

    try:
        statements = sqlglot.parse(schema_sql, read="postgres")
    except Exception as exc:
        raise SchemaResolutionError(f"Could not parse tracked PostgreSQL schema: {exc}") from exc

    relation_columns: dict[str, frozenset[str]] = {}
    for statement in statements:
        if not isinstance(statement, sqlglot.expressions.Create):
            continue
        if str(statement.args.get("kind") or "").upper() != "TABLE":
            continue
        schema = statement.this
        if not isinstance(schema, sqlglot.expressions.Schema) or not isinstance(
            schema.this,
            sqlglot.expressions.Table,
        ):
            raise SchemaResolutionError("Tracked CREATE TABLE statement has no table schema")
        relation = postgres_identifier_name(schema.this.this)
        if not relation:
            raise SchemaResolutionError("Tracked CREATE TABLE statement has an empty relation name")
        if relation in relation_columns:
            raise SchemaResolutionError(f"Tracked schema contains duplicate relation {relation!r}")

        columns: set[str] = set()
        for expression in schema.expressions:
            if not isinstance(expression, sqlglot.expressions.ColumnDef):
                continue
            column = postgres_identifier_name(expression.this)
            if not column:
                raise SchemaResolutionError(f"Tracked relation {relation!r} has an empty column name")
            if column in columns:
                raise SchemaResolutionError(
                    f"Tracked relation {relation!r} contains duplicate column {column!r}"
                )
            columns.add(column)
        if not columns:
            raise SchemaResolutionError(f"Tracked relation {relation!r} has no parsed columns")
        relation_columns[relation] = frozenset(columns)

    if not relation_columns:
        raise SchemaResolutionError("Tracked schema contains no CREATE TABLE columns")
    return relation_columns


def extract_schema_aware_join_edges(
    sql: str,
    alias_map: Mapping[str, str],
    relation_columns: Mapping[str, frozenset[str]],
) -> frozenset[tuple[str, str]]:

    structure = analyze_query_structure(sql, relation_columns)
    if structure.alias_map != dict(alias_map):
        raise SchemaResolutionError("Caller alias map differs from the schema-aware SQL structure")
    return structure.join_edges


def analyze_query_structure(
    sql: str,
    relation_columns: Mapping[str, frozenset[str]] | None = None,
) -> QueryStructure:

    if relation_columns is None:
        try:
            expression = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            expression = None
        root_scope = _root_scope(expression, strict=False) if expression is not None else None
        excluded_query_block_count = _excluded_query_block_count(root_scope)
        if excluded_query_block_count:
            raise SchemaResolutionError(
                "Queries with nested query blocks require a tracked schema and explicit scope validation"
            )
        alias_map = _alias_map_from_scope(root_scope, strict=False) if root_scope is not None else {}
        referenced = _schema_less_referenced_columns_by_alias(root_scope, set(alias_map))
        predicate_catalog = _build_predicate_catalog(
            expression,
            root_scope,
            alias_map,
            column_owners=None,
        )
        support_reasons = _exact_cardinality_query_reasons(
            expression,
            root_scope,
            predicate_catalog,
            non_hintable_query_blocks=(),
        )
        return QueryStructure(
            query_sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            alias_map=alias_map,
            join_edges=extract_join_edges(sql, set(alias_map)),
            referenced_columns_by_alias=referenced,
            non_hintable_query_blocks=(),
            resolution_receipt=ColumnResolutionReceipt(
                mode="qualified_only",
                hintable_scope="root_query_block",
                scope_policy=ROOT_QUERY_SCOPE_POLICY,
                parser_name="sqlglot",
                parser_version=sqlglot.__version__,
                dialect=SQLGLOT_DIALECT,
                query_block_count=1,
                excluded_query_block_count=0,
                relation_occurrence_count=len(alias_map),
                non_hintable_relation_occurrence_count=0,
                correlated_column_count=0,
                total_resolved_column_count=sum(len(columns) for columns in referenced.values()),
                schema_catalog_sha256="",
                alias_count=len(alias_map),
                qualified_column_count=sum(len(columns) for columns in referenced.values()),
                unqualified_column_count=0,
                resolved_column_count=sum(len(columns) for columns in referenced.values()),
            ),
            predicate_catalog=predicate_catalog,
            exact_cardinality_supported=not support_reasons,
            exact_cardinality_reason_codes=support_reasons,
        )

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:
        raise SchemaResolutionError(f"Could not parse SQL for schema-aware join resolution: {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise SchemaResolutionError("Schema-aware join resolution requires exactly one SQL statement")
    expression = statements[0]
    if not isinstance(expression, sqlglot.expressions.Select):
        raise SchemaResolutionError("Schema-aware join resolution supports one SELECT statement")
    if expression.args.get("with_") is not None:
        raise SchemaResolutionError("Schema-aware join resolution does not yet support CTEs")
    root_scope = _root_scope(expression, strict=True)
    assert root_scope is not None
    if any(not isinstance(source, sqlglot.expressions.Table) for source in root_scope.sources.values()):
        raise SchemaResolutionError(
            "Schema-aware join resolution does not yet support derived, CTE, LATERAL, or table-function sources"
        )

    alias_map = _alias_map_from_scope(root_scope, strict=True)
    normalized_alias_map = _validate_schema_resolution_inputs(alias_map, relation_columns)
    non_hintable_blocks, non_hintable_local_column_count, correlated_column_count = (
        _validate_non_hintable_query_blocks(root_scope, normalized_alias_map, relation_columns)
    )
    output_names = _select_output_names(expression)
    referenced_columns: dict[str, set[str]] = {alias: set() for alias in normalized_alias_map}
    column_owners: dict[int, str] = {}
    qualified_count = 0
    unqualified_count = 0
    for column in _root_resolution_columns(root_scope, expression):
        output_clause = _bare_output_name_clause(column, expression)
        output_name = postgres_identifier_name(column.this)
        output_name_count = output_names.get(output_name, 0)
        if output_clause == "order" and output_name_count:
            if output_name_count != 1:
                raise SchemaResolutionError(
                    f"ORDER BY output column {output_name!r} is ambiguous across {output_name_count} projections"
                )
            continue
        if output_clause == "group" and output_name_count:
            input_owners = _unqualified_column_owners(output_name, normalized_alias_map, relation_columns)
            if not input_owners:
                if output_name_count != 1:
                    raise SchemaResolutionError(
                        f"GROUP BY output column {output_name!r} is ambiguous across {output_name_count} projections"
                    )
                continue
        owner, was_qualified = _resolve_column_owner(column, normalized_alias_map, relation_columns)
        column_name = postgres_identifier_name(column.this)
        referenced_columns[owner].add(column_name)
        column_owners[id(column)] = owner
        if was_qualified:
            qualified_count += 1
        else:
            unqualified_count += 1

    comparison_types = (
        sqlglot.expressions.EQ,
        sqlglot.expressions.NEQ,
        sqlglot.expressions.GT,
        sqlglot.expressions.GTE,
        sqlglot.expressions.LT,
        sqlglot.expressions.LTE,
    )
    edges: set[tuple[str, str]] = set()
    for predicate in root_scope.find_all(*comparison_types):
        owners = {
            column_owners[id(column)]
            for column in predicate.find_all(sqlglot.expressions.Column)
            if id(column) in column_owners
        }
        for left, right in itertools.combinations(sorted(owners), 2):
            edges.add((left, right))

    canonical_catalog = {
        relation: sorted(columns)
        for relation, columns in sorted(relation_columns.items())
    }
    predicate_catalog = _build_predicate_catalog(
        expression,
        root_scope,
        alias_map,
        column_owners=column_owners,
    )
    support_reasons = _exact_cardinality_query_reasons(
        expression,
        root_scope,
        predicate_catalog,
        non_hintable_query_blocks=non_hintable_blocks,
    )
    return QueryStructure(
        query_sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        alias_map=alias_map,
        join_edges=frozenset(edges),
        referenced_columns_by_alias={
            alias: frozenset(columns)
            for alias, columns in referenced_columns.items()
        },
        non_hintable_query_blocks=non_hintable_blocks,
        resolution_receipt=ColumnResolutionReceipt(
            mode="tracked_schema_unique_owner",
            hintable_scope="root_query_block",
            scope_policy=ROOT_QUERY_SCOPE_POLICY,
            parser_name="sqlglot",
            parser_version=sqlglot.__version__,
            dialect=SQLGLOT_DIALECT,
            query_block_count=1 + len(non_hintable_blocks),
            excluded_query_block_count=len(non_hintable_blocks),
            relation_occurrence_count=len(alias_map) + sum(len(block.alias_map) for block in non_hintable_blocks),
            non_hintable_relation_occurrence_count=sum(len(block.alias_map) for block in non_hintable_blocks),
            correlated_column_count=correlated_column_count,
            total_resolved_column_count=(
                qualified_count + unqualified_count + non_hintable_local_column_count
            ),
            schema_catalog_sha256=canonical_json_sha256(canonical_catalog),
            alias_count=len(alias_map),
            qualified_column_count=qualified_count,
            unqualified_column_count=unqualified_count,
            resolved_column_count=qualified_count + unqualified_count,
        ),
        predicate_catalog=predicate_catalog,
        exact_cardinality_supported=not support_reasons,
        exact_cardinality_reason_codes=support_reasons,
    )


def applicable_predicate_sha256(
    structure: QueryStructure,
    aliases: set[str] | frozenset[str] | tuple[str, ...],
) -> str:

    alias_set = set(aliases)
    applicable = [
        {
            "predicate_id": item.predicate_id,
            "normalized_sql": item.normalized_sql,
            "referenced_aliases": list(item.referenced_aliases),
            "join_semantics": item.join_semantics,
        }
        for item in structure.predicate_catalog
        if item.eligible_for_pushdown and set(item.referenced_aliases).issubset(alias_set)
    ]
    return canonical_json_sha256(applicable)


def _build_predicate_catalog(
    expression: Any,
    root_scope: Scope | None,
    alias_map: Mapping[str, str],
    *,
    column_owners: Mapping[int, str] | None,
) -> tuple[PredicateCatalogEntry, ...]:
    if not isinstance(expression, sqlglot.expressions.Select) or root_scope is None:
        return ()
    root_column_ids = {id(column) for column in root_scope.columns}
    raw: list[tuple[Any, str]] = []
    where = expression.args.get("where")
    if isinstance(where, sqlglot.expressions.Where):
        raw.extend((item, "where") for item in _split_conjuncts(where.this))
    for join in expression.args.get("joins") or []:
        if not isinstance(join, sqlglot.expressions.Join):
            continue
        on = join.args.get("on")
        if on is None:
            continue
        side = str(join.args.get("side") or "").strip().lower()
        kind = str(join.args.get("kind") or "").strip().lower()
        semantics = f"{side or kind or 'inner'}_on"
        raw.extend((item, semantics) for item in _split_conjuncts(on))

    rows: list[PredicateCatalogEntry] = []
    for predicate, semantics in raw:
        aliases: set[str] = set()
        unresolved_column = False
        for column in predicate.find_all(sqlglot.expressions.Column):
            if id(column) not in root_column_ids:
                continue
            owner = column_owners.get(id(column)) if column_owners is not None else None
            if owner is None:
                owner = postgres_identifier_name(column.args.get("table"))
            if owner and owner in alias_map:
                aliases.add(owner)
            else:
                unresolved_column = True
        normalized = predicate.sql(dialect=SQLGLOT_DIALECT, pretty=False, normalize=True)
        volatility = _predicate_volatility(predicate)
        if unresolved_column:
            volatility = "scope_unknown"
        identity = {
            "normalized_sql": normalized,
            "referenced_aliases": sorted(aliases),
            "join_semantics": semantics,
        }
        rows.append(
            PredicateCatalogEntry(
                predicate_id=f"pred:{canonical_json_sha256(identity)[:24]}",
                normalized_sql=normalized,
                referenced_aliases=tuple(sorted(aliases)),
                volatility_class=volatility,
                join_semantics=semantics,
                eligible_for_pushdown=(
                    volatility == "immutable" and semantics in {"where", "inner_on", "cross_on"}
                ),
            )
        )
    rows.sort(
        key=lambda item: (
            item.normalized_sql,
            item.join_semantics,
            item.referenced_aliases,
            item.predicate_id,
        )
    )
    return tuple(rows)


def _split_conjuncts(expression: Any) -> tuple[Any, ...]:
    if isinstance(expression, sqlglot.expressions.And):
        return (*_split_conjuncts(expression.this), *_split_conjuncts(expression.expression))
    return (expression,)


def _predicate_volatility(expression: Any) -> str:
    functions = tuple(expression.find_all(sqlglot.expressions.Func))
    return "immutable" if not functions else "unknown"


_VOLATILE_OR_SET_RETURNING_FUNCTIONS = frozenset(
    {
        "random",
        "clock_timestamp",
        "timeofday",
        "nextval",
        "setval",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "generate_series",
        "unnest",
    }
)


def _contains_volatile_or_set_returning_function(expression: Any) -> bool:
    for function in expression.find_all(sqlglot.expressions.Func):
        names = {
            str(getattr(function, "name", "") or "").casefold(),
            str(function.sql_name() or "").casefold(),
        }
        if names & _VOLATILE_OR_SET_RETURNING_FUNCTIONS:
            return True
    return False


def _exact_cardinality_query_reasons(
    expression: Any,
    root_scope: Scope | None,
    predicate_catalog: tuple[PredicateCatalogEntry, ...],
    *,
    non_hintable_query_blocks: tuple[NonHintableQueryBlock, ...],
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if not isinstance(expression, sqlglot.expressions.Select) or root_scope is None:
        reasons.add("unsupported_root_query_block")
        return tuple(sorted(reasons))
    if non_hintable_query_blocks:
        reasons.add("nested_query_block")
    if expression.args.get("with_") is not None:
        reasons.add("cte_query")
    if expression.args.get("limit") is not None:
        reasons.add("query_level_limit")
    if expression.args.get("offset") is not None:
        reasons.add("query_level_offset")
    if any(
        not isinstance(source, sqlglot.expressions.Table)
        for source in root_scope.sources.values()
    ):
        reasons.add("non_base_table_source")
    for join in expression.args.get("joins") or []:
        if not isinstance(join, sqlglot.expressions.Join):
            reasons.add("unsupported_join_expression")
            continue
        side = str(join.args.get("side") or "").strip().upper()
        kind = str(join.args.get("kind") or "").strip().upper()
        method = str(join.args.get("method") or "").strip().upper()
        if side or kind not in {"", "INNER", "CROSS"}:
            reasons.add("non_inner_join")
        if method == "NATURAL":
            reasons.add("natural_join")
    if any(item.volatility_class != "immutable" for item in predicate_catalog):
        reasons.add("predicate_scope_or_volatility_unknown")
    if _contains_volatile_or_set_returning_function(expression):
        reasons.add("volatile_or_set_returning_expression")
    return tuple(sorted(reasons))


def _select_output_names(expression: sqlglot.expressions.Select) -> dict[str, int]:

    counts: dict[str, int] = {}
    for projection in expression.expressions:
        if isinstance(projection, sqlglot.expressions.Alias):
            name = postgres_identifier_name(projection.args.get("alias"))
        elif isinstance(projection, sqlglot.expressions.Column):
            name = postgres_identifier_name(projection.this)
        else:
            name = ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _bare_output_name_clause(
    column: sqlglot.expressions.Column,
    expression: sqlglot.expressions.Select,
) -> str | None:

    if postgres_identifier_name(column.args.get("table")):
        return None

    parent = column.parent
    order = expression.args.get("order")
    if (
        isinstance(parent, sqlglot.expressions.Ordered)
        and parent.this is column
        and parent.parent is order
    ):
        return "order"

    group = expression.args.get("group")
    if parent is group and column in group.expressions:
        return "group"
    return None


def _validate_schema_resolution_inputs(
    alias_map: Mapping[str, str],
    relation_columns: Mapping[str, frozenset[str]],
) -> dict[str, str]:

    if not alias_map:
        raise SchemaResolutionError("Schema-aware join resolution requires at least one query relation")
    normalized_alias_map: dict[str, str] = {}
    for alias, relation in alias_map.items():
        if not isinstance(alias, str) or not alias or not isinstance(relation, str) or not relation:
            raise SchemaResolutionError("Schema-aware join resolution received an invalid alias map")
        if alias in normalized_alias_map:
            raise SchemaResolutionError(f"Query contains duplicate relation alias {alias!r}")
        if relation not in relation_columns:
            raise SchemaResolutionError(
                f"Tracked schema has no columns for query relation {relation!r} (alias {alias!r})"
            )
        normalized_alias_map[alias] = relation
    return normalized_alias_map


def _resolve_column_owner(
    column: sqlglot.expressions.Column,
    alias_map: Mapping[str, str],
    relation_columns: Mapping[str, frozenset[str]],
) -> tuple[str, bool]:
    column_name = postgres_identifier_name(column.this)
    if not column_name:
        raise SchemaResolutionError("Query contains an empty column reference")
    qualifier = postgres_identifier_name(column.args.get("table"))
    if qualifier:
        relation = alias_map.get(qualifier)
        if relation is None:
            raise SchemaResolutionError(f"Query references unknown relation alias {qualifier!r}")
        if column_name not in relation_columns[relation]:
            raise SchemaResolutionError(
                f"Tracked relation {relation!r} has no column {column_name!r} for alias {qualifier!r}"
            )
        return qualifier, True

    owners = _unqualified_column_owners(column_name, alias_map, relation_columns)
    if not owners:
        raise SchemaResolutionError(f"Unqualified column {column_name!r} is unknown in the query relation scope")
    if len(owners) != 1:
        raise SchemaResolutionError(
            f"Unqualified column {column_name!r} is ambiguous across aliases {sorted(owners)!r}"
        )
    return owners[0], False


def _unqualified_column_owners(
    column_name: str,
    alias_map: Mapping[str, str],
    relation_columns: Mapping[str, frozenset[str]],
) -> list[str]:
    return [alias for alias, relation in alias_map.items() if column_name in relation_columns[relation]]


def _root_scope(
    expression: sqlglot.expressions.Expression,
    *,
    strict: bool,
) -> Scope | None:
    try:
        scope = build_scope(expression)
    except Exception as exc:
        if strict:
            raise SchemaResolutionError(f"Could not build SQL query scope: {exc}") from exc
        return None
    if scope is None:
        if strict:
            raise SchemaResolutionError("Could not build a root SQL query scope")
        return None
    return scope


def _excluded_query_block_count(scope: Scope | None) -> int:
    return max(0, len(list(scope.traverse())) - 1) if scope is not None else 0


def _root_resolution_columns(
    scope: Scope,
    expression: sqlglot.expressions.Select,
) -> tuple[sqlglot.expressions.Column, ...]:
    columns = list(scope.columns)
    seen = {id(column) for column in columns}
    for clause_name in ("order", "group"):
        clause = expression.args.get(clause_name)
        if not isinstance(clause, sqlglot.expressions.Expression):
            continue
        for column in clause.find_all(sqlglot.expressions.Column):
            if id(column) not in seen:
                columns.append(column)
                seen.add(id(column))
    return tuple(columns)


def _validate_non_hintable_query_blocks(
    root_scope: Scope,
    root_alias_map: Mapping[str, str],
    relation_columns: Mapping[str, frozenset[str]],
) -> tuple[tuple[NonHintableQueryBlock, ...], int, int]:
    children = list(root_scope.subquery_scopes)
    traversed_children = [scope for scope in root_scope.traverse() if scope is not root_scope]
    if len(traversed_children) != len(children) or {id(scope) for scope in traversed_children} != {
        id(scope) for scope in children
    }:
        raise SchemaResolutionError(
            "Schema-aware query scope supports only direct root correlated EXISTS blocks"
        )

    blocks: list[NonHintableQueryBlock] = []
    local_column_count = 0
    correlated_column_count = 0
    comparison_types = (
        sqlglot.expressions.EQ,
        sqlglot.expressions.NEQ,
        sqlglot.expressions.GT,
        sqlglot.expressions.GTE,
        sqlglot.expressions.LT,
        sqlglot.expressions.LTE,
    )
    root_aliases = set(root_alias_map)
    for index, child in enumerate(children):
        parent_expression = child.expression.parent
        if (
            child.parent is not root_scope
            or not child.is_subquery
            or not isinstance(parent_expression, sqlglot.expressions.Exists)
            or parent_expression.this is not child.expression
        ):
            raise SchemaResolutionError(
                "Schema-aware query scope supports only direct root correlated EXISTS blocks"
            )
        if any(
            (
                child.subquery_scopes,
                child.derived_table_scopes,
                child.cte_scopes,
                child.union_scopes,
                child.udtf_scopes,
                child.lateral_sources,
            )
        ):
            raise SchemaResolutionError("Nested EXISTS scopes cannot contain nested or derived query sources")

        local_alias_map = _alias_map_from_scope(child, strict=True)
        if len(local_alias_map) != 1:
            raise SchemaResolutionError("Each supported correlated EXISTS block must contain exactly one base table")
        shadowed = sorted(root_aliases.intersection(local_alias_map))
        if shadowed:
            raise SchemaResolutionError(
                f"Correlated EXISTS alias shadows a root relation alias: {shadowed!r}"
            )
        if not child.is_correlated_subquery:
            raise SchemaResolutionError(
                "Schema-aware query scope supports only direct root correlated EXISTS blocks"
            )
        normalized_local_alias_map = _validate_schema_resolution_inputs(local_alias_map, relation_columns)
        combined_alias_map = {**root_alias_map, **normalized_local_alias_map}
        local_aliases = set(normalized_local_alias_map)
        local_referenced: dict[str, set[str]] = {alias: set() for alias in normalized_local_alias_map}
        outer_referenced: dict[str, set[str]] = {}
        owner_by_column: dict[int, str] = {}
        outer_column_ids: set[int] = set()
        for column in child.find_all(sqlglot.expressions.Column):
            if not postgres_identifier_name(column.args.get("table")):
                raise SchemaResolutionError("Correlated EXISTS blocks require qualified column references")
            owner, _was_qualified = _resolve_column_owner(column, combined_alias_map, relation_columns)
            column_name = postgres_identifier_name(column.this)
            owner_by_column[id(column)] = owner
            if owner in local_aliases:
                local_referenced[owner].add(column_name)
                local_column_count += 1
            else:
                outer_referenced.setdefault(owner, set()).add(column_name)
                outer_column_ids.add(id(column))
                correlated_column_count += 1

        external_column_ids = {id(column) for column in child.external_columns}
        if external_column_ids != outer_column_ids:
            raise SchemaResolutionError("Correlated EXISTS external-column scope classification mismatch")

        local_edges: set[tuple[str, str]] = set()
        correlation_edges: set[tuple[str, str]] = set()
        consumed_outer_column_ids: set[int] = set()
        for predicate in child.find_all(*comparison_types):
            predicate_columns = list(predicate.find_all(sqlglot.expressions.Column))
            owners = {
                owner_by_column[id(column)]
                for column in predicate_columns
                if id(column) in owner_by_column
            }
            local_owners = owners.intersection(local_aliases)
            outer_owners = owners.intersection(root_aliases)
            if len(outer_owners) > 1 or (outer_owners and not local_owners):
                raise SchemaResolutionError(
                    "Correlated EXISTS comparisons must be local/local or local/root"
                )
            for left, right in itertools.combinations(sorted(local_owners), 2):
                local_edges.add((left, right))
            for local_alias in sorted(local_owners):
                for outer_alias in sorted(outer_owners):
                    correlation_edges.add((local_alias, outer_alias))
            consumed_outer_column_ids.update(
                id(column)
                for column in predicate_columns
                if id(column) in outer_column_ids
            )
        if consumed_outer_column_ids != outer_column_ids:
            raise SchemaResolutionError(
                "Correlated EXISTS outer references require a supported local/root comparison"
            )

        normalized_sql = child.expression.sql(dialect=SQLGLOT_DIALECT)
        blocks.append(
            NonHintableQueryBlock(
                scope_id=f"root.exists[{index}]",
                parent_scope_id="root",
                kind="correlated_exists",
                normalized_sql_sha256=hashlib.sha256(normalized_sql.encode("utf-8")).hexdigest(),
                alias_map=dict(normalized_local_alias_map),
                referenced_columns_by_alias={
                    alias: frozenset(columns)
                    for alias, columns in local_referenced.items()
                },
                outer_referenced_columns_by_alias={
                    alias: frozenset(columns)
                    for alias, columns in outer_referenced.items()
                },
                join_edges=frozenset(local_edges),
                correlation_edges=frozenset(correlation_edges),
            )
        )
    return tuple(blocks), local_column_count, correlated_column_count


def _alias_map_from_scope(
    scope: Scope,
    *,
    strict: bool,
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for _selected_alias, (node, source) in scope.selected_sources.items():
        if not isinstance(node, sqlglot.expressions.Table) or not isinstance(source, sqlglot.expressions.Table):
            if strict:
                raise SchemaResolutionError(
                    "Query root or nested scope contains a derived, CTE, LATERAL, table-function, or VALUES source"
                )
            continue
        table = source
        name = postgres_identifier_name(table.this)
        alias_expression = node.args.get("alias")
        alias = postgres_identifier_name(alias_expression.this) if alias_expression is not None else name
        if not name or not alias:
            if strict:
                raise SchemaResolutionError("Query contains a table with an empty name or alias")
            continue
        if strict and alias in aliases:
            raise SchemaResolutionError(f"Query contains duplicate relation alias {alias!r}")
        aliases[alias] = name
    if strict and not aliases:
        raise SchemaResolutionError("Schema-aware join resolution requires base-table sources")
    return aliases


def _schema_less_referenced_columns_by_alias(
    scope: Scope | None,
    aliases: set[str],
) -> dict[str, frozenset[str]]:
    if scope is None:
        return {}
    referenced: dict[str, set[str]] = {alias: set() for alias in aliases}
    only_alias = next(iter(aliases)) if len(aliases) == 1 else ""
    for column in scope.columns:
        qualifier = postgres_identifier_name(column.args.get("table"))
        column_name = postgres_identifier_name(column.this)
        alias = qualifier if qualifier in aliases else only_alias if not qualifier else ""
        if alias and column_name:
            referenced.setdefault(alias, set()).add(column_name)
    return {alias: frozenset(columns) for alias, columns in referenced.items()}


def postgres_identifier_name(identifier: Any) -> str:
    if isinstance(identifier, sqlglot.expressions.Identifier):
        name = str(identifier.this or "")
        return name if identifier.args.get("quoted") else name.lower()
    return str(identifier or "")


def build_plan_summary(explain_payload: Any, fallback_digest: str | None = None) -> PlanSummary:
    payload = normalize_explain_payload(explain_payload)
    top = payload[0] if isinstance(payload, list) and payload else payload
    plan = top.get("Plan", top) if isinstance(top, dict) else {}
    digest = fallback_digest or plan_digest(payload)

    table_accesses: list[dict[str, Any]] = []
    join_alias_sets: list[tuple[str, ...]] = []
    join_methods: list[JoinMethodObservation] = []
    estimation_errors: list[EstimationError] = []
    cardinality_observations: list[EstimationError] = []
    hotspots: list[RuntimeHotspot] = []

    def visit(node: dict[str, Any]) -> set[str]:
        child_aliases: set[str] = set()
        child_alias_groups: list[tuple[str, ...]] = []
        for child in node.get("Plans", []) or []:
            if isinstance(child, dict):
                child_group = visit(child)
                child_aliases.update(child_group)
                if child_group:
                    child_alias_groups.append(tuple(sorted(child_group)))

        alias = str(node.get("Alias") or node.get("Relation Name") or "").strip()
        aliases = set(child_aliases)
        if alias:
            aliases.add(alias)

        node_type = str(node.get("Node Type") or "operator")
        if any(marker in node_type for marker in _SCAN_NODE_MARKERS):
            table_accesses.append(
                {
                    "alias": alias,
                    "relation": node.get("Relation Name"),
                    "node_type": node_type,
                    "index_name": node.get("Index Name"),
                    "filter": node.get("Filter"),
                    "index_cond": node.get("Index Cond"),
                    "actual_rows_per_loop": _as_float(node.get("Actual Rows")),
                    "actual_loops": _as_float(node.get("Actual Loops")),
                    "rows_removed_by_filter_per_loop": _as_float(node.get("Rows Removed by Filter")),
                }
            )

        if node_type in _JOIN_NODE_TYPES and len(aliases) >= 2:
            join_alias_sets.append(tuple(sorted(aliases)))
            join_methods.append(
                JoinMethodObservation(
                    aliases=tuple(sorted(aliases)),
                    node_type=node_type,
                    join_type=str(node.get("Join Type") or ""),
                    child_aliases=tuple(child_alias_groups),
                )
            )

        plan_rows = _as_float(node.get("Plan Rows"))
        actual_rows = _actual_rows(node)
        if plan_rows is not None and actual_rows is not None:
            ratio = actual_rows / plan_rows if plan_rows > 0 else 0.0
            observation = EstimationError(
                aliases=tuple(sorted(aliases)),
                node_type=node_type,
                plan_rows=plan_rows,
                actual_rows=actual_rows,
                ratio=ratio,
            )
            cardinality_observations.append(observation)
            if ratio >= _ESTIMATION_ERROR_THRESHOLD or ratio <= 1.0 / _ESTIMATION_ERROR_THRESHOLD:
                estimation_errors.append(observation)

        total_time = _total_actual_time_ms(node)
        if total_time is not None and total_time > 0:
            hotspots.append(
                RuntimeHotspot(
                    aliases=tuple(sorted(aliases)),
                    node_type=node_type,
                    total_time_ms=total_time,
                    actual_rows=actual_rows,
                    loops=_as_int(node.get("Actual Loops")),
                )
            )
        return aliases

    aliases = tuple(sorted(visit(plan))) if isinstance(plan, dict) else ()
    estimation_errors.sort(key=lambda item: _estimation_error_magnitude(item.ratio), reverse=True)
    hotspots.sort(key=lambda item: item.total_time_ms, reverse=True)

    execution_time = _as_float(top.get("Execution Time")) if isinstance(top, dict) else None
    planning_time = _as_float(top.get("Planning Time")) if isinstance(top, dict) else None

    return PlanSummary(
        plan_digest=digest,
        shape=_shape_text(plan),
        aliases=aliases,
        join_alias_sets=tuple(_dedupe_tuple_list(join_alias_sets)),
        join_order_signature=_join_order_signature(plan),
        join_methods=tuple(join_methods),
        table_accesses=tuple(table_accesses),
        estimation_errors=tuple(estimation_errors[:8]),
        cardinality_observations=tuple(cardinality_observations),
        runtime_hotspots=tuple(hotspots[:8]),
        total_cost=_as_float(plan.get("Total Cost")) if isinstance(plan, dict) else None,
        execution_time_seconds=(execution_time / 1000.0) if execution_time is not None else None,
        planning_time_seconds=(planning_time / 1000.0) if planning_time is not None else None,
    )


def normalize_explain_payload(explain_payload: Any) -> Any:
    if isinstance(explain_payload, str):
        return json.loads(explain_payload)
    return explain_payload


def plan_decision_diff(reference: PlanSummary, candidate: PlanSummary) -> dict[str, Any]:
    reference_relations = {tuple(sorted(aliases)) for aliases in reference.join_alias_sets}
    candidate_relations = {tuple(sorted(aliases)) for aliases in candidate.join_alias_sets}

    def methods(summary: PlanSummary) -> dict[tuple[str, ...], list[tuple[str, str]]]:
        result: dict[tuple[str, ...], list[tuple[str, str]]] = {}
        for item in summary.join_methods:
            result.setdefault(tuple(sorted(item.aliases)), []).append((item.node_type, item.join_type))
        return {key: sorted(value) for key, value in result.items()}

    def accesses(summary: PlanSummary) -> dict[str, list[tuple[str, str]]]:
        result: dict[str, list[tuple[str, str]]] = {}
        for item in summary.table_accesses:
            alias = str(item.get("alias") or item.get("relation") or "")
            result.setdefault(alias, []).append((str(item.get("node_type") or ""), str(item.get("index_name") or "")))
        return {key: sorted(value) for key, value in result.items()}

    before_methods, after_methods = methods(reference), methods(candidate)
    before_accesses, after_accesses = accesses(reference), accesses(candidate)
    unattributed_access_changed = before_accesses.pop("", []) != after_accesses.pop("", [])
    return {
        "scope": "visible_plan_decisions_not_performance_equivalence_or_causal_effect",
        "reference_plan_digest": reference.plan_digest,
        "candidate_plan_digest": candidate.plan_digest,
        "join_order_changed": (reference.join_order_signature != candidate.join_order_signature
                               if reference.join_order_signature and candidate.join_order_signature else None),
        "added_join_relation_count": len(candidate_relations - reference_relations),
        "removed_join_relation_count": len(reference_relations - candidate_relations),
        "changed_shared_join_method_targets": [list(key) for key in sorted(before_methods.keys() & after_methods.keys())
                                               if before_methods[key] != after_methods[key]],
        "changed_access_aliases": [key for key in sorted(before_accesses.keys() | after_accesses.keys())
                                   if before_accesses.get(key) != after_accesses.get(key)],
        "unattributed_access_changed": unattributed_access_changed,
    }


def plan_digest(explain_payload: Any) -> str:
    payload = normalize_explain_payload(explain_payload)
    canonical = _canonicalize_explain(payload)
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _canonicalize_explain(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_canonicalize_explain(item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    if "Plan" in payload:
        return {"Plan": _canonicalize_explain(payload["Plan"])}

    keep_keys = (
        "Node Type",
        "Schema",
        "Output",
        "Scan Direction",
        "Operation",
        "Inner Unique",
        "Subplan Name",
        "CTE Name",
        "Function Name",
        "Table Function Name",
        "Function Call",
        "One-Time Filter",
        "Cache Key",
        "Cache Mode",
        "Presorted Key",
        "Sampling Method",
        "Sampling Parameters",
        "Repeatable Seed",
        "Conflict Resolution",
        "Conflict Arbiter Indexes",
        "Parent Relationship",
        "Join Type",
        "Relation Name",
        "Alias",
        "Index Name",
        "Hash Cond",
        "Merge Cond",
        "Join Filter",
        "Index Cond",
        "Recheck Cond",
        "Filter",
        "Group Key",
        "Sort Key",
        "Strategy",
        "Partial Mode",
        "Parallel Aware",
        "Workers Planned",
        "Async Capable",
    )
    out = {key: payload.get(key) for key in keep_keys if key in payload}
    children = payload.get("Plans")
    if isinstance(children, list):
        out["Plans"] = [_canonicalize_explain(child) for child in children]
    return out


def _shape_text(node: Any, limit: int = 900) -> str:
    text = _shape_text_inner(node)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _join_order_signature(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    children = [child for child in node.get("Plans", []) or [] if isinstance(child, dict)]
    node_type = str(node.get("Node Type") or "")
    child_signatures = [_join_order_signature(child) for child in children]
    if node_type in _JOIN_NODE_TYPES and child_signatures:
        return f"({','.join(child_signatures)})"
    alias = str(node.get("Alias") or node.get("Relation Name") or "").strip()
    if alias:
        return alias
    nonempty = [signature for signature in child_signatures if signature]
    if len(nonempty) == 1:
        return nonempty[0]
    return f"({','.join(nonempty)})" if nonempty else ""


def _shape_text_inner(node: Any) -> str:
    if not isinstance(node, dict):
        return "?"
    node_type = str(node.get("Node Type") or "?")
    label = node_type
    alias = node.get("Alias") or node.get("Relation Name")
    if alias:
        label += f"[{alias}]"
    index_name = node.get("Index Name")
    if index_name:
        label += f"<{index_name}>"
    children = node.get("Plans") or []
    if not children:
        return label
    return f"{label}({', '.join(_shape_text_inner(child) for child in children if isinstance(child, dict))})"


def _actual_rows(node: dict[str, Any]) -> float | None:
    rows = _as_float(node.get("Actual Rows"))
    if rows is None:
        return None
    loops = _as_int(node.get("Actual Loops"))
    if loops is not None and loops <= 0:
        return None
    return rows


def _total_actual_time_ms(node: dict[str, Any]) -> float | None:
    total = _as_float(node.get("Actual Total Time"))
    if total is None:
        return None
    loops = _as_int(node.get("Actual Loops"))
    if loops is not None and loops <= 0:
        return None
    loops = loops or 1
    return total * loops


def _estimation_error_magnitude(ratio: float) -> float:

    if ratio <= 0:
        return float("inf")
    return max(ratio, 1.0 / ratio)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value)))
    except ValueError:
        return None


def _dedupe_tuple_list(items: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
