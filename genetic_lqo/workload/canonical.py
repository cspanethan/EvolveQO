from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from genetic_lqo.core.artifacts import canonical_json_sha256

from .task_contract import TaskContract, get_task_contract_for_workload
from .workloads import WorkloadQuery, WorkloadSet, get_workload_set

CANONICAL_WORKLOAD_SCHEMA_VERSION = 1
CANONICAL_WORKLOAD_KIND = "tracked_canonical_query"


@dataclass(frozen=True, init=False)
class CanonicalWorkloadContext:

    workload_set: str
    query_name: str
    payload_sha256: str
    _payload_json: str
    _schema_relation_columns_json: str

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError("Use CanonicalWorkloadContext.from_tracked()")

    @classmethod
    def from_tracked(
        cls,
        workload: WorkloadSet,
        task: TaskContract,
        query: WorkloadQuery,
    ) -> CanonicalWorkloadContext:

        if not isinstance(workload, WorkloadSet):
            raise TypeError("workload must be a WorkloadSet")
        if not isinstance(task, TaskContract):
            raise TypeError("task must be a TaskContract")
        if not isinstance(query, WorkloadQuery):
            raise TypeError("query must be a WorkloadQuery")

        tracked_workload = get_workload_set(workload.name)
        tracked_task = get_task_contract_for_workload(tracked_workload)
        tracked_query = tracked_workload.queries.get(query.name)
        if tracked_query is None:
            raise ValueError(f"Query {query.name!r} is not in tracked workload {workload.name!r}")
        if workload.identity_payload() != tracked_workload.identity_payload():
            raise ValueError("Workload identity does not match the tracked workload")
        if task.identity_payload() != tracked_task.identity_payload():
            raise ValueError("Task identity does not match the tracked task contract")
        if query.identity_payload() != tracked_query.identity_payload() or query.query_sql != tracked_query.query_sql:
            raise ValueError("Query identity or SQL does not match the tracked workload member")

        payload = _payload_from_components(tracked_workload, tracked_task, tracked_query)
        payload_json = json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
        instance = object.__new__(cls)
        object.__setattr__(instance, "workload_set", tracked_workload.name)
        object.__setattr__(instance, "query_name", tracked_query.name)
        object.__setattr__(instance, "payload_sha256", canonical_json_sha256(payload))
        object.__setattr__(instance, "_payload_json", payload_json)
        object.__setattr__(
            instance,
            "_schema_relation_columns_json",
            json.dumps(
                {
                    relation: sorted(columns)
                    for relation, columns in sorted(tracked_query.schema_relation_columns.items())
                },
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return instance

    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CANONICAL_WORKLOAD_SCHEMA_VERSION,
            "kind": CANONICAL_WORKLOAD_KIND,
            "workload_set": self.workload_set,
            "query_name": self.query_name,
            "payload_sha256": self.payload_sha256,
        }

    def require_query(self, query_name: str, query_sql: str) -> None:
        query = self.payload().get("query")
        if not isinstance(query, dict):
            raise ValueError("Canonical workload context has no query identity")
        if query_name != self.query_name or query.get("name") != query_name:
            raise ValueError("Canonical workload query name does not match the search query")
        expected_sha256 = query.get("query_sql_sha256")
        actual_sha256 = hashlib.sha256(query_sql.encode("utf-8")).hexdigest()
        if not isinstance(expected_sha256, str) or actual_sha256 != expected_sha256:
            raise ValueError("Canonical workload query SQL hash does not match the tracked query")

    def require_connection(self, database_name: str | None, database_user: str | None) -> None:
        database = self.payload().get("database")
        if not isinstance(database, dict):
            raise ValueError("Canonical workload context has no database identity")
        expected_name = database.get("name")
        expected_user = database.get("user")
        if not isinstance(expected_name, str) or not expected_name:
            raise ValueError("Canonical workload database name must be nonempty")
        if not isinstance(expected_user, str) or not expected_user:
            raise ValueError("Canonical workload database user must be nonempty")
        if database_name != expected_name:
            raise ValueError(
                f"Canonical workload requires database {expected_name!r}, got {database_name!r}"
            )
        if database_user != expected_user:
            raise ValueError(
                f"Canonical workload requires database user {expected_user!r}, got {database_user!r}"
            )

    def schema_relation_columns(self) -> dict[str, frozenset[str]] | None:

        raw_columns = json.loads(self._schema_relation_columns_json)
        if not isinstance(raw_columns, dict):
            raise ValueError("Canonical workload schema snapshot is malformed")
        columns: dict[str, frozenset[str]] = {}
        for relation, relation_columns in raw_columns.items():
            if (
                not isinstance(relation, str)
                or not relation
                or not isinstance(relation_columns, list)
                or not relation_columns
                or any(not isinstance(column, str) or not column for column in relation_columns)
                or len(relation_columns) != len(set(relation_columns))
            ):
                raise ValueError("Canonical workload schema snapshot is malformed")
            columns[relation] = frozenset(relation_columns)
        return columns or None


def tracked_canonical_workload_payload(workload_set: str, query_name: str) -> dict[str, Any]:

    workload = get_workload_set(workload_set)
    task = get_task_contract_for_workload(workload)
    query = workload.queries.get(query_name)
    if query is None:
        raise ValueError(f"Query {query_name!r} is not in tracked workload {workload.name!r}")
    return _payload_from_components(workload, task, query)


def canonical_workload_manifest_payload(payload: dict[str, Any]) -> dict[str, Any]:

    workload_set = payload.get("workload_set")
    query = payload.get("query")
    query_name = query.get("name") if isinstance(query, dict) else None
    if not isinstance(workload_set, str) or not workload_set or not isinstance(query_name, str) or not query_name:
        raise ValueError("Canonical workload payload has an invalid workload or query identity")
    return {
        "schema_version": CANONICAL_WORKLOAD_SCHEMA_VERSION,
        "kind": CANONICAL_WORKLOAD_KIND,
        "workload_set": workload_set,
        "query_name": query_name,
        "payload_sha256": canonical_json_sha256(payload),
    }


def _payload_from_components(
    workload: WorkloadSet,
    task: TaskContract,
    query: WorkloadQuery,
) -> dict[str, Any]:
    if task.workload_set != workload.name:
        raise ValueError("Task and workload editions differ")
    if query.db != workload.db or query.db_user != workload.db_user:
        raise ValueError("Query and workload database identities differ")
    task_weight = task.weights.get(query.name)
    if type(task_weight) is not int or task_weight <= 0:
        raise ValueError(f"Query {query.name!r} is absent from the tracked task contract")
    query_payload = query.identity_payload()
    query_payload["task_weight"] = task_weight
    database_user = workload.db_user
    if not database_user:
        raise ValueError(f"Tracked workload {workload.name!r} has no canonical database user")
    return {
        "schema_version": CANONICAL_WORKLOAD_SCHEMA_VERSION,
        "kind": CANONICAL_WORKLOAD_KIND,
        "workload_set": workload.name,
        "workload": workload.identity_payload(),
        "task": task.identity_payload(),
        "query": query_payload,
        "database": {
            "name": workload.db,
            "user": database_user,
        },
    }
