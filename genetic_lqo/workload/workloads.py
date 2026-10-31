from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from genetic_lqo.core.artifacts import canonical_json_sha256
from genetic_lqo.planning.plan import parse_schema_relation_columns

WORKLOAD_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = WORKLOAD_ROOT.parents[1]
AVAILABLE_WORKLOAD_SETS = (
    "JOB",
    "CEB_3K",
    "DSB",
    "STACK_SO_PAST",
    "STACK_SO_FUTURE",
    "STACK_SO_SHIFTED",
)
IMPLICIT_WORKLOAD_SETS = frozenset({"JOB"})

JOB_SOURCE_PATH = "genetic_lqo/workload/job"
IMDB_SCHEMA_SOURCE_PATH = f"{JOB_SOURCE_PATH}/schema.sql"
IMDB_SCHEMA_SHA256 = "c06949ecd4c1640c1a75e178bbd8f9391696740377331506f62edf64641bee56"

CEB_3K_SOURCE_PATH = "genetic_lqo/workload/ceb/ceb_3k.zip"
CEB_3K_ARCHIVE_SHA256 = "bd4a7d0c05adc2e17f65e209ab6f2d5a0ca0f2465d0bedbe66858bdd9852edf0"
CEB_3K_ARCHIVE_ENTRY_COUNT = 3_149
CEB_3K_QUERY_COUNT = 3_133
CEB_3K_DIRECTORY_COUNT = 16
CEB_3K_TOTAL_QUERY_BYTES = 3_186_235
CEB_3K_MAX_QUERY_BYTES = 2_912
CEB_3K_MAX_COMPRESSION_RATIO = 3.002906976744186
CEB_3K_DIRECTORIES = (
    "1a/",
    "2a/",
    "2b/",
    "2c/",
    "3a/",
    "3b/",
    "4a/",
    "5a/",
    "6a/",
    "7a/",
    "8a/",
    "9a/",
    "9b/",
    "10a/",
    "11a/",
    "11b/",
)

DSB_SOURCE_PATH = "genetic_lqo/workload/dsb/dsb.zip"
DSB_SCHEMA_SOURCE_PATH = "genetic_lqo/workload/dsb/schema.sql"
DSB_ARCHIVE_SHA256 = "26184e128280c366e77610130c5116b17caad7a41e8dada2b61a566d4c5f2846"
DSB_SCHEMA_SHA256 = "567a6aaf9311602649cf967bc28b31c37a4d644a9537b3f593d330939643a8b1"
DSB_ARCHIVE_ENTRY_COUNT = 631
DSB_QUERY_COUNT = 90
DSB_NORUN_COUNT = 510
DSB_DIRECTORY_COUNT = 30
DSB_TOTAL_QUERY_BYTES = 91_691
DSB_MAX_QUERY_BYTES = 2_028
DSB_MAX_QUERY_COMPRESSION_RATIO = 3.9412955465587043
DSB_TOTAL_NORUN_BYTES = 519_358
DSB_MAX_NORUN_BYTES = 2_032
DSB_MAX_NORUN_COMPRESSION_RATIO = 3.9815573770491803
DSB_ARCHIVE_SCHEMA_BYTES = 30_001
DSB_ARCHIVE_SCHEMA_COMPRESSION_RATIO = 7.210045662100456
DSB_TEMPLATES = (
    "013",
    "018",
    "019",
    "025",
    "027",
    "040",
    "050",
    "072",
    "084",
    "085",
    "091",
    "099",
    "100",
    "101",
    "102",
)
DSB_DIRECTORIES = tuple(
    directory
    for template in DSB_TEMPLATES
    for directory in (f"query{template}/", f"query{template}_spj/")
)

STACK_SOURCE_PATH = "genetic_lqo/workload/stack/stack.zip"
STACK_SCHEMA_SOURCE_PATH = "genetic_lqo/workload/stack/schema.sql"
STACK_UPSTREAM_REPOSITORY = "https://github.com/Speculative/BayesQO.git"
STACK_UPSTREAM_REVISION = "fa8a58f6d0e3f424db945d393a89431fcdc3464c"
STACK_ARCHIVE_SHA256 = "2be3dc9428381bac284d0e4b09726cace8395e1caab43d91c2deb6a4442dabf6"
STACK_SCHEMA_SHA256 = "021d47345626a2461eacf1eae67b9836bba10cd82d8a1ec59854ea04f52d2bd2"
STACK_ARCHIVE_ENTRY_COUNT = 6_208
STACK_QUERY_COUNT = 6_191
STACK_DIRECTORY_COUNT = 16
STACK_TOTAL_QUERY_BYTES = 4_154_867
STACK_MAX_QUERY_BYTES = 1_156
STACK_MAX_QUERY_COMPRESSION_RATIO = 3.334862385321101
STACK_SCHEMA_BYTES = 12_836
STACK_SCHEMA_COMPRESSION_RATIO = 7.436848203939745
STACK_DIRECTORIES = tuple(f"q{template}/" for template in range(1, 17))
STACK_DATABASE_BY_EDITION = {
    "STACK_SO_PAST": "so_past",
    "STACK_SO_FUTURE": "so_future",
    "STACK_SO_SHIFTED": "so_shift",
}

_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 4_000
_MAX_MEMBER_BYTES = 4 * 1024
_MAX_TOTAL_QUERY_BYTES = 4 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 10.0
_MAX_MEMBER_NAME_BYTES = 128
_MAX_SCHEMA_BYTES = 1024 * 1024
_CEB_DIRECTORY_RE = re.compile(r"[1-9][0-9]?[a-z]/\Z", re.ASCII)
_CEB_MEMBER_RE = re.compile(r"(?P<family>[1-9][0-9]?[a-z])/(?P=family)(?P<ordinal>[0-9]+)\.sql\Z", re.ASCII)
_DEFAULT_QUERY_NAME_PATTERN = r"[A-Z][A-Z0-9]*_[A-Z0-9]+\Z"
_DSB_QUERY_NAME_PATTERN = r"DSB_[0-9]{3}(?:_SPJ)?_[0-2]\Z"
_DSB_DIRECTORY_RE = re.compile(r"query[0-9]{3}(?:_spj)?/\Z", re.ASCII)
_DSB_MEMBER_RE = re.compile(
    r"(?P<family>query(?P<template>[0-9]{3})(?P<spj>_spj)?)/"
    r"(?P=family)_(?P<instance>[0-9]+)\.sql(?P<norun>\.norun)?\Z",
    re.ASCII,
)
_STACK_QUERY_NAME_PATTERN = r"STACK_Q(?:[1-9]|1[0-6])-[0-9]{3,4}\Z"
_STACK_DIRECTORY_RE = re.compile(r"q(?:[1-9]|1[0-6])/\Z", re.ASCII)
_STACK_MEMBER_RE = re.compile(
    r"(?P<family>q(?:[1-9]|1[0-6]))/(?P=family)-(?P<ordinal>[0-9]{3,4})\.sql\Z",
    re.ASCII,
)


@dataclass(frozen=True)
class UpstreamGitSource:

    repository_url: str
    revision: str
    repo_relative_path: str
    raw_sha256: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.repo_relative_path)
        if not self.repository_url.startswith("https://"):
            raise ValueError("Upstream authority repository URL must use HTTPS")
        if re.fullmatch(r"[0-9a-f]{40}", self.revision, re.ASCII) is None:
            raise ValueError("Upstream authority revision must be a lowercase 40-hex Git commit")
        if re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256, re.ASCII) is None:
            raise ValueError("Upstream authority raw SHA-256 must be lowercase 64-hex")
        if (
            not self.repo_relative_path
            or path.is_absolute()
            or str(path) != self.repo_relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in self.repo_relative_path
        ):
            raise ValueError("Upstream authority path must be a normalized POSIX repository-relative path")

    def identity_payload(self) -> dict[str, str]:
        return {
            "kind": "git_revision",
            "repository_url": self.repository_url,
            "revision": self.revision,
            "repo_relative_path": self.repo_relative_path,
            "raw_sha256": self.raw_sha256,
        }


STACK_ARCHIVE_AUTHORITY = UpstreamGitSource(
    repository_url=STACK_UPSTREAM_REPOSITORY,
    revision=STACK_UPSTREAM_REVISION,
    repo_relative_path=STACK_SOURCE_PATH,
    raw_sha256=STACK_ARCHIVE_SHA256,
)


@dataclass(frozen=True)
class WorkloadQuery:
    name: str
    query_sql: str
    db: str
    schema_path: Path
    source_kind: str = ""
    source_path: str = ""
    source_raw_sha256: str = ""
    source_member: str | None = None
    source_member_raw_sha256: str | None = None
    schema_source_path: str = ""
    schema_raw_sha256: str = ""
    db_user: str = ""
    schema_relation_columns: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    source_authority: UpstreamGitSource | None = None

    @property
    def query_sql_sha256(self) -> str:
        return hashlib.sha256(self.query_sql.encode("utf-8")).hexdigest()

    def identity_payload(self) -> dict[str, Any]:

        payload = {
            "name": self.name,
            "database": self.db,
            "database_user": self.db_user,
            "query_sql_sha256": self.query_sql_sha256,
            "source": {
                "kind": self.source_kind,
                "repo_relative_path": self.source_path,
                "raw_sha256": self.source_raw_sha256,
                "member": self.source_member,
                "member_raw_sha256": self.source_member_raw_sha256,
            },
            "schema": {
                "repo_relative_path": self.schema_source_path,
                "raw_sha256": self.schema_raw_sha256,
            },
        }
        if self.schema_relation_columns:
            payload["join_resolution"] = _schema_resolution_payload(self.schema_relation_columns)
        if self.source_authority is not None:
            if self.source_authority.raw_sha256 != self.source_raw_sha256:
                raise ValueError("Query upstream authority hash differs from its tracked source hash")
            payload["source"]["upstream_authority"] = self.source_authority.identity_payload()
        return payload


@dataclass(frozen=True)
class WorkloadSet:
    name: str
    db: str
    schema_path: Path
    queries: dict[str, WorkloadQuery]
    source_kind: str = ""
    source_path: str = ""
    source_raw_sha256: str = ""
    schema_source_path: str = ""
    schema_raw_sha256: str = ""
    source_entry_count: int = 0
    source_query_count: int = 0
    source_directory_count: int = 0
    source_total_query_bytes: int = 0
    db_user: str = ""
    query_name_pattern: str = _DEFAULT_QUERY_NAME_PATTERN
    schema_relation_columns: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    source_ignored_count: int = 0
    source_authority: UpstreamGitSource | None = None

    def identity_payload(self) -> dict[str, Any]:

        payload = {
            "schema_version": 1,
            "workload_set": self.name,
            "database": self.db,
            "database_user": self.db_user,
            "source": {
                "kind": self.source_kind,
                "repo_relative_path": self.source_path,
                "raw_sha256": self.source_raw_sha256,
                "entry_count": self.source_entry_count,
                "query_count": self.source_query_count,
                "directory_count": self.source_directory_count,
                "total_query_bytes": self.source_total_query_bytes,
            },
            "schema": {
                "repo_relative_path": self.schema_source_path,
                "raw_sha256": self.schema_raw_sha256,
            },
        }
        if self.query_name_pattern != _DEFAULT_QUERY_NAME_PATTERN:
            payload["query_name_pattern"] = self.query_name_pattern
        if self.schema_relation_columns:
            payload["join_resolution"] = _schema_resolution_payload(self.schema_relation_columns)
        if self.source_ignored_count:
            payload["source"]["ignored_count"] = self.source_ignored_count
        if self.source_authority is not None:
            if self.source_authority.raw_sha256 != self.source_raw_sha256:
                raise ValueError("Workload upstream authority hash differs from its tracked source hash")
            payload["source"]["upstream_authority"] = self.source_authority.identity_payload()
        return payload


@dataclass(frozen=True)
class _ZipArchiveContract:
    raw_sha256: str
    entry_count: int
    query_count: int
    directory_count: int
    total_query_bytes: int
    max_query_bytes: int
    max_compression_ratio: float
    directories: tuple[str, ...]
    max_archive_bytes: int = _MAX_ARCHIVE_BYTES
    max_entries: int = _MAX_ARCHIVE_ENTRIES
    max_member_bytes: int = _MAX_MEMBER_BYTES
    max_total_query_bytes: int = _MAX_TOTAL_QUERY_BYTES
    max_compression_ratio_limit: float = _MAX_COMPRESSION_RATIO


@dataclass(frozen=True)
class _DsbArchiveContract:
    raw_sha256: str
    entry_count: int
    query_count: int
    norun_count: int
    directory_count: int
    total_query_bytes: int
    max_query_bytes: int
    max_query_compression_ratio: float
    total_norun_bytes: int
    max_norun_bytes: int
    max_norun_compression_ratio: float
    schema_bytes: int
    schema_sha256: str
    schema_compression_ratio: float
    directories: tuple[str, ...]
    max_archive_bytes: int = 1024 * 1024
    max_entries: int = 700
    max_member_bytes: int = 4 * 1024
    max_schema_bytes: int = 64 * 1024
    max_total_member_bytes: int = 1024 * 1024
    max_compression_ratio_limit: float = _MAX_COMPRESSION_RATIO


@dataclass(frozen=True)
class _StackArchiveContract:
    raw_sha256: str
    entry_count: int
    query_count: int
    directory_count: int
    total_query_bytes: int
    max_query_bytes: int
    max_query_compression_ratio: float
    schema_bytes: int
    schema_sha256: str
    schema_compression_ratio: float
    directories: tuple[str, ...]
    max_archive_bytes: int = 4 * 1024 * 1024
    max_entries: int = 6_500
    max_member_bytes: int = 2 * 1024
    max_schema_bytes: int = 64 * 1024
    max_total_member_bytes: int = 5 * 1024 * 1024
    max_compression_ratio_limit: float = _MAX_COMPRESSION_RATIO


_CEB_3K_CONTRACT = _ZipArchiveContract(
    raw_sha256=CEB_3K_ARCHIVE_SHA256,
    entry_count=CEB_3K_ARCHIVE_ENTRY_COUNT,
    query_count=CEB_3K_QUERY_COUNT,
    directory_count=CEB_3K_DIRECTORY_COUNT,
    total_query_bytes=CEB_3K_TOTAL_QUERY_BYTES,
    max_query_bytes=CEB_3K_MAX_QUERY_BYTES,
    max_compression_ratio=CEB_3K_MAX_COMPRESSION_RATIO,
    directories=CEB_3K_DIRECTORIES,
)

_DSB_CONTRACT = _DsbArchiveContract(
    raw_sha256=DSB_ARCHIVE_SHA256,
    entry_count=DSB_ARCHIVE_ENTRY_COUNT,
    query_count=DSB_QUERY_COUNT,
    norun_count=DSB_NORUN_COUNT,
    directory_count=DSB_DIRECTORY_COUNT,
    total_query_bytes=DSB_TOTAL_QUERY_BYTES,
    max_query_bytes=DSB_MAX_QUERY_BYTES,
    max_query_compression_ratio=DSB_MAX_QUERY_COMPRESSION_RATIO,
    total_norun_bytes=DSB_TOTAL_NORUN_BYTES,
    max_norun_bytes=DSB_MAX_NORUN_BYTES,
    max_norun_compression_ratio=DSB_MAX_NORUN_COMPRESSION_RATIO,
    schema_bytes=DSB_ARCHIVE_SCHEMA_BYTES,
    schema_sha256=DSB_SCHEMA_SHA256,
    schema_compression_ratio=DSB_ARCHIVE_SCHEMA_COMPRESSION_RATIO,
    directories=DSB_DIRECTORIES,
)

_STACK_CONTRACT = _StackArchiveContract(
    raw_sha256=STACK_ARCHIVE_SHA256,
    entry_count=STACK_ARCHIVE_ENTRY_COUNT,
    query_count=STACK_QUERY_COUNT,
    directory_count=STACK_DIRECTORY_COUNT,
    total_query_bytes=STACK_TOTAL_QUERY_BYTES,
    max_query_bytes=STACK_MAX_QUERY_BYTES,
    max_query_compression_ratio=STACK_MAX_QUERY_COMPRESSION_RATIO,
    schema_bytes=STACK_SCHEMA_BYTES,
    schema_sha256=STACK_SCHEMA_SHA256,
    schema_compression_ratio=STACK_SCHEMA_COMPRESSION_RATIO,
    directories=STACK_DIRECTORIES,
)


def get_workload_set(workload_set: str) -> WorkloadSet:
    normalized = workload_set.strip().upper()
    if normalized == "JOB":
        return _load_job()
    if normalized == "CEB_3K":
        return _load_ceb_3k()
    if normalized == "DSB":
        return _load_dsb()
    if normalized in STACK_DATABASE_BY_EDITION:
        return _load_stack_edition(normalized)
    choices = ", ".join(AVAILABLE_WORKLOAD_SETS)
    raise ValueError(f"genetic_lqo bundles only {choices} workloads; got {workload_set!r}")


def infer_implicit_workload_set(query_name: str) -> str:

    if not isinstance(query_name, str) or not query_name:
        raise ValueError("Canonical workload query name must be a nonempty string")
    implicit_matches = [
        workload_name
        for workload_name in sorted(IMPLICIT_WORKLOAD_SETS)
        if query_name in get_workload_set(workload_name).queries
    ]
    if len(implicit_matches) == 1:
        return implicit_matches[0]
    raise ValueError(
        "Canonical queries outside the implicit registry require an explicit edition via --workload-set; "
        f"query={query_name!r}; an explicit edition will reject unknown membership"
    )


def _load_job() -> WorkloadSet:
    job_dir = WORKLOAD_ROOT / "job"
    schema_path = job_dir / "schema.sql"
    _verify_schema(schema_path)
    queries: dict[str, WorkloadQuery] = {}
    source_ledger: list[dict[str, str]] = []
    total_query_bytes = 0
    for path in sorted(job_dir.glob("*.sql")):
        if path.name == "schema.sql":
            continue
        raw = _read_regular_bytes(path, max_bytes=_MAX_MEMBER_BYTES)
        query_sql = _decode_utf8_round_trip(raw, path.as_posix())
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        source_path = f"{JOB_SOURCE_PATH}/{path.name}"
        name = "JOB_" + path.stem.upper()
        queries[name] = WorkloadQuery(
            name=name,
            query_sql=query_sql,
            db="imdb",
            schema_path=schema_path,
            db_user="imdb",
            source_kind="file",
            source_path=source_path,
            source_raw_sha256=raw_sha256,
            schema_source_path=IMDB_SCHEMA_SOURCE_PATH,
            schema_raw_sha256=IMDB_SCHEMA_SHA256,
        )
        source_ledger.append({"repo_relative_path": source_path, "raw_sha256": raw_sha256})
        total_query_bytes += len(raw)
    return WorkloadSet(
        name="JOB",
        db="imdb",
        schema_path=schema_path,
        queries=queries,
        db_user="imdb",
        source_kind="directory_ledger",
        source_path=JOB_SOURCE_PATH,
        source_raw_sha256=canonical_json_sha256(source_ledger),
        schema_source_path=IMDB_SCHEMA_SOURCE_PATH,
        schema_raw_sha256=IMDB_SCHEMA_SHA256,
        source_entry_count=len(queries),
        source_query_count=len(queries),
        source_total_query_bytes=total_query_bytes,
    )


def _load_ceb_3k() -> WorkloadSet:
    archive_path = REPOSITORY_ROOT / CEB_3K_SOURCE_PATH
    schema_path = REPOSITORY_ROOT / IMDB_SCHEMA_SOURCE_PATH
    _verify_schema(schema_path)
    members = _load_ceb_archive(archive_path, _CEB_3K_CONTRACT)
    queries = {
        name: WorkloadQuery(
            name=name,
            query_sql=query_sql,
            db="imdb",
            schema_path=schema_path,
            db_user="imdb",
            source_kind="zip_member",
            source_path=CEB_3K_SOURCE_PATH,
            source_raw_sha256=CEB_3K_ARCHIVE_SHA256,
            source_member=member_name,
            source_member_raw_sha256=member_sha256,
            schema_source_path=IMDB_SCHEMA_SOURCE_PATH,
            schema_raw_sha256=IMDB_SCHEMA_SHA256,
        )
        for name, (member_name, query_sql, member_sha256) in sorted(members.items())
    }
    return WorkloadSet(
        name="CEB_3K",
        db="imdb",
        schema_path=schema_path,
        queries=queries,
        db_user="imdb",
        source_kind="zip_archive",
        source_path=CEB_3K_SOURCE_PATH,
        source_raw_sha256=CEB_3K_ARCHIVE_SHA256,
        schema_source_path=IMDB_SCHEMA_SOURCE_PATH,
        schema_raw_sha256=IMDB_SCHEMA_SHA256,
        source_entry_count=CEB_3K_ARCHIVE_ENTRY_COUNT,
        source_query_count=CEB_3K_QUERY_COUNT,
        source_directory_count=CEB_3K_DIRECTORY_COUNT,
        source_total_query_bytes=CEB_3K_TOTAL_QUERY_BYTES,
    )


def _load_dsb() -> WorkloadSet:
    archive_path = REPOSITORY_ROOT / DSB_SOURCE_PATH
    schema_path = REPOSITORY_ROOT / DSB_SCHEMA_SOURCE_PATH
    schema_raw = _verify_schema(
        schema_path,
        expected_sha256=DSB_SCHEMA_SHA256,
        source_path=DSB_SCHEMA_SOURCE_PATH,
    )
    schema_sql = _decode_utf8_round_trip(schema_raw, DSB_SCHEMA_SOURCE_PATH)
    schema_relation_columns = parse_schema_relation_columns(schema_sql)
    members = _load_dsb_archive(
        archive_path,
        schema_raw=schema_raw,
        contract=_DSB_CONTRACT,
    )
    queries = {
        name: WorkloadQuery(
            name=name,
            query_sql=query_sql,
            db="dsb_sf50",
            schema_path=schema_path,
            db_user="so",
            source_kind="zip_member",
            source_path=DSB_SOURCE_PATH,
            source_raw_sha256=DSB_ARCHIVE_SHA256,
            source_member=member_name,
            source_member_raw_sha256=member_sha256,
            schema_source_path=DSB_SCHEMA_SOURCE_PATH,
            schema_raw_sha256=DSB_SCHEMA_SHA256,
            schema_relation_columns=schema_relation_columns,
        )
        for name, (member_name, query_sql, member_sha256) in sorted(members.items())
    }
    return WorkloadSet(
        name="DSB",
        db="dsb_sf50",
        schema_path=schema_path,
        queries=queries,
        db_user="so",
        source_kind="zip_archive",
        source_path=DSB_SOURCE_PATH,
        source_raw_sha256=DSB_ARCHIVE_SHA256,
        schema_source_path=DSB_SCHEMA_SOURCE_PATH,
        schema_raw_sha256=DSB_SCHEMA_SHA256,
        source_entry_count=DSB_ARCHIVE_ENTRY_COUNT,
        source_query_count=DSB_QUERY_COUNT,
        source_directory_count=DSB_DIRECTORY_COUNT,
        source_total_query_bytes=DSB_TOTAL_QUERY_BYTES,
        query_name_pattern=_DSB_QUERY_NAME_PATTERN,
        schema_relation_columns=schema_relation_columns,
        source_ignored_count=DSB_NORUN_COUNT,
    )


def _load_stack_edition(edition: str) -> WorkloadSet:
    archive_path = REPOSITORY_ROOT / STACK_SOURCE_PATH
    schema_path = REPOSITORY_ROOT / STACK_SCHEMA_SOURCE_PATH
    schema_raw = _verify_schema(
        schema_path,
        expected_sha256=STACK_SCHEMA_SHA256,
        source_path=STACK_SCHEMA_SOURCE_PATH,
    )
    schema_sql = _decode_utf8_round_trip(schema_raw, STACK_SCHEMA_SOURCE_PATH)
    schema_relation_columns = parse_schema_relation_columns(schema_sql)
    members = _load_stack_archive(
        archive_path,
        schema_raw=schema_raw,
        contract=_STACK_CONTRACT,
    )
    database = STACK_DATABASE_BY_EDITION[edition]
    queries = {
        name: WorkloadQuery(
            name=name,
            query_sql=query_sql,
            db=database,
            schema_path=schema_path,
            db_user="so",
            source_kind="zip_member",
            source_path=STACK_SOURCE_PATH,
            source_raw_sha256=STACK_ARCHIVE_SHA256,
            source_member=member_name,
            source_member_raw_sha256=member_sha256,
            schema_source_path=STACK_SCHEMA_SOURCE_PATH,
            schema_raw_sha256=STACK_SCHEMA_SHA256,
            schema_relation_columns=schema_relation_columns,
            source_authority=STACK_ARCHIVE_AUTHORITY,
        )
        for name, (member_name, query_sql, member_sha256) in sorted(members.items())
    }
    return WorkloadSet(
        name=edition,
        db=database,
        schema_path=schema_path,
        queries=queries,
        db_user="so",
        source_kind="zip_archive",
        source_path=STACK_SOURCE_PATH,
        source_raw_sha256=STACK_ARCHIVE_SHA256,
        schema_source_path=STACK_SCHEMA_SOURCE_PATH,
        schema_raw_sha256=STACK_SCHEMA_SHA256,
        source_entry_count=STACK_ARCHIVE_ENTRY_COUNT,
        source_query_count=STACK_QUERY_COUNT,
        source_directory_count=STACK_DIRECTORY_COUNT,
        source_total_query_bytes=STACK_TOTAL_QUERY_BYTES,
        query_name_pattern=_STACK_QUERY_NAME_PATTERN,
        schema_relation_columns=schema_relation_columns,
        source_authority=STACK_ARCHIVE_AUTHORITY,
    )


def _load_ceb_archive(
    archive_path: Path,
    contract: _ZipArchiveContract,
) -> dict[str, tuple[str, str, str]]:

    raw_archive = _read_regular_bytes(archive_path, max_bytes=contract.max_archive_bytes)
    archive_sha256 = hashlib.sha256(raw_archive).hexdigest()
    if archive_sha256 != contract.raw_sha256:
        raise ValueError(
            f"CEB archive raw SHA-256 mismatch: expected={contract.raw_sha256} actual={archive_sha256}"
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_archive), mode="r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid CEB ZIP archive: {exc}") from exc

    with archive:
        if archive.comment:
            raise ValueError("CEB ZIP archive comments are not allowed")
        infos = archive.infolist()
        if len(infos) > contract.max_entries:
            raise ValueError(f"CEB archive entry limit exceeded: {len(infos)} > {contract.max_entries}")
        if len(infos) != contract.entry_count:
            raise ValueError(f"CEB archive entry count mismatch: expected={contract.entry_count} actual={len(infos)}")

        seen_members: set[str] = set()
        seen_queries: set[str] = set()
        directories: list[str] = []
        query_infos: list[tuple[zipfile.ZipInfo, str]] = []
        declared_total = 0
        declared_max = 0
        observed_max_ratio = 0.0

        for info in infos:
            member_name = info.filename
            _validate_member_path(member_name)
            if member_name in seen_members:
                raise ValueError(f"CEB archive contains duplicate member: {member_name}")
            seen_members.add(member_name)
            if info.flag_bits & 0x1:
                raise ValueError(f"CEB archive contains encrypted member: {member_name}")
            if info.create_system != 3:
                raise ValueError(f"CEB archive member lacks Unix file type metadata: {member_name}")

            mode = info.external_attr >> 16
            member_type = stat.S_IFMT(mode)
            if info.is_dir():
                if member_type != stat.S_IFDIR or not _CEB_DIRECTORY_RE.fullmatch(member_name):
                    raise ValueError(f"CEB archive contains invalid directory entry: {member_name}")
                if info.file_size != 0 or info.compress_size != 0 or info.compress_type != zipfile.ZIP_STORED:
                    raise ValueError(f"CEB archive directory carries data: {member_name}")
                directories.append(member_name)
                continue

            if member_type != stat.S_IFREG:
                raise ValueError(f"CEB archive contains non-regular member: {member_name}")
            match = _CEB_MEMBER_RE.fullmatch(member_name)
            if match is None or len(PurePosixPath(member_name).parts) != 2:
                raise ValueError(f"CEB archive contains invalid SQL member path: {member_name}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"CEB SQL member uses unsupported compression: {member_name}")
            if info.file_size <= 0 or info.file_size > contract.max_member_bytes:
                raise ValueError(f"CEB SQL member size is outside bounds: {member_name} ({info.file_size})")
            if info.compress_size <= 0:
                raise ValueError(f"CEB SQL member has invalid compressed size: {member_name}")
            ratio = info.file_size / info.compress_size
            if ratio > contract.max_compression_ratio_limit:
                raise ValueError(f"CEB SQL member compression ratio is outside bounds: {member_name} ({ratio})")

            query_name = "CEB_" + PurePosixPath(member_name).stem.upper()
            if query_name in seen_queries:
                raise ValueError(f"CEB archive contains duplicate normalized query ID: {query_name}")
            seen_queries.add(query_name)
            declared_total += info.file_size
            if declared_total > contract.max_total_query_bytes:
                raise ValueError(
                    f"CEB archive total SQL size exceeds bound: {declared_total} > {contract.max_total_query_bytes}"
                )
            declared_max = max(declared_max, info.file_size)
            observed_max_ratio = max(observed_max_ratio, ratio)
            query_infos.append((info, query_name))

        if tuple(sorted(directories)) != tuple(sorted(contract.directories)):
            raise ValueError("CEB archive directory inventory mismatch")
        if len(directories) != contract.directory_count:
            raise ValueError(
                f"CEB archive directory count mismatch: expected={contract.directory_count} actual={len(directories)}"
            )
        if len(query_infos) != contract.query_count:
            raise ValueError(
                f"CEB archive SQL member count mismatch: expected={contract.query_count} actual={len(query_infos)}"
            )
        if declared_total != contract.total_query_bytes:
            raise ValueError(
                f"CEB archive total SQL size mismatch: expected={contract.total_query_bytes} actual={declared_total}"
            )
        if declared_max != contract.max_query_bytes:
            raise ValueError(
                f"CEB archive maximum SQL size mismatch: expected={contract.max_query_bytes} actual={declared_max}"
            )
        if observed_max_ratio != contract.max_compression_ratio:
            raise ValueError(
                "CEB archive maximum compression ratio mismatch: "
                f"expected={contract.max_compression_ratio} actual={observed_max_ratio}"
            )

        loaded: dict[str, tuple[str, str, str]] = {}
        actual_total = 0
        for info, query_name in query_infos:
            try:
                with archive.open(info, mode="r") as member_file:
                    member_raw = member_file.read(contract.max_member_bytes + 1)
            except (RuntimeError, zipfile.BadZipFile) as exc:
                raise ValueError(f"Could not read CEB SQL member {info.filename}: {exc}") from exc
            if len(member_raw) != info.file_size:
                raise ValueError(
                    "CEB SQL member declared/actual size mismatch: "
                    f"{info.filename} ({info.file_size} != {len(member_raw)})"
                )
            query_sql = _decode_utf8_round_trip(member_raw, info.filename)
            member_sha256 = hashlib.sha256(member_raw).hexdigest()
            loaded[query_name] = (info.filename, query_sql, member_sha256)
            actual_total += len(member_raw)
        if actual_total != declared_total:
            raise ValueError(f"CEB archive actual SQL size mismatch: expected={declared_total} actual={actual_total}")
        return loaded


def _load_dsb_archive(
    archive_path: Path,
    *,
    schema_raw: bytes,
    contract: _DsbArchiveContract,
) -> dict[str, tuple[str, str, str]]:

    raw_archive = _read_regular_bytes(archive_path, max_bytes=contract.max_archive_bytes)
    archive_sha256 = hashlib.sha256(raw_archive).hexdigest()
    if archive_sha256 != contract.raw_sha256:
        raise ValueError(
            f"DSB archive raw SHA-256 mismatch: expected={contract.raw_sha256} actual={archive_sha256}"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_archive), mode="r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid DSB ZIP archive: {exc}") from exc

    with archive:
        if archive.comment:
            raise ValueError("DSB ZIP archive comments are not allowed")
        infos = archive.infolist()
        if len(infos) > contract.max_entries:
            raise ValueError(f"DSB archive entry limit exceeded: {len(infos)} > {contract.max_entries}")
        if len(infos) != contract.entry_count:
            raise ValueError(f"DSB archive entry count mismatch: expected={contract.entry_count} actual={len(infos)}")

        seen_members: set[str] = set()
        directories: list[str] = []
        runnable_infos: list[tuple[zipfile.ZipInfo, str]] = []
        norun_infos: list[zipfile.ZipInfo] = []
        schema_info: zipfile.ZipInfo | None = None
        family_runnable_instances: dict[str, set[int]] = {}
        family_norun_instances: dict[str, set[int]] = {}
        declared_total = 0

        for info in infos:
            member_name = info.filename
            _validate_member_path(member_name, label="DSB")
            if member_name in seen_members:
                raise ValueError(f"DSB archive contains duplicate member: {member_name}")
            seen_members.add(member_name)
            if info.flag_bits & 0x1:
                raise ValueError(f"DSB archive contains encrypted member: {member_name}")
            if info.create_system != 3:
                raise ValueError(f"DSB archive member lacks Unix file type metadata: {member_name}")

            mode = info.external_attr >> 16
            member_type = stat.S_IFMT(mode)
            if info.is_dir():
                if member_type != stat.S_IFDIR or _DSB_DIRECTORY_RE.fullmatch(member_name) is None:
                    raise ValueError(f"DSB archive contains invalid directory entry: {member_name}")
                if info.file_size != 0 or info.compress_size != 0 or info.compress_type != zipfile.ZIP_STORED:
                    raise ValueError(f"DSB archive directory carries data: {member_name}")
                directories.append(member_name)
                continue

            if member_type != stat.S_IFREG:
                raise ValueError(f"DSB archive contains non-regular member: {member_name}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"DSB archive member uses unsupported compression: {member_name}")
            member_limit = contract.max_schema_bytes if member_name == "schema.sql" else contract.max_member_bytes
            if info.file_size <= 0 or info.file_size > member_limit:
                raise ValueError(f"DSB archive member size is outside bounds: {member_name} ({info.file_size})")
            if info.compress_size <= 0:
                raise ValueError(f"DSB archive member has invalid compressed size: {member_name}")
            ratio = info.file_size / info.compress_size
            if ratio > contract.max_compression_ratio_limit:
                raise ValueError(f"DSB archive member compression ratio is outside bounds: {member_name} ({ratio})")
            declared_total += info.file_size
            if declared_total > contract.max_total_member_bytes:
                raise ValueError("DSB archive total member size exceeds bound")

            if member_name == "schema.sql":
                if schema_info is not None:
                    raise ValueError("DSB archive contains duplicate root schema")
                schema_info = info
                continue

            match = _DSB_MEMBER_RE.fullmatch(member_name)
            if match is None or len(PurePosixPath(member_name).parts) != 2:
                raise ValueError(f"DSB archive contains invalid SQL member path: {member_name}")
            family = match.group("family")
            instance = int(match.group("instance"))
            is_norun = match.group("norun") is not None
            instance_map = family_norun_instances if is_norun else family_runnable_instances
            instances = instance_map.setdefault(family, set())
            if instance in instances:
                raise ValueError(f"DSB archive contains duplicate family instance: {member_name}")
            instances.add(instance)
            if is_norun:
                norun_infos.append(info)
                continue

            template = match.group("template")
            spj = "_SPJ" if match.group("spj") else ""
            query_name = f"DSB_{template}{spj}_{instance}"
            runnable_infos.append((info, query_name))

        if tuple(sorted(directories)) != tuple(sorted(contract.directories)):
            raise ValueError("DSB archive directory inventory mismatch")
        if len(directories) != contract.directory_count:
            raise ValueError(
                f"DSB archive directory count mismatch: expected={contract.directory_count} actual={len(directories)}"
            )
        expected_families = {directory.rstrip("/") for directory in contract.directories}
        if set(family_runnable_instances) != expected_families or set(family_norun_instances) != expected_families:
            raise ValueError("DSB archive query family inventory mismatch")
        for family in sorted(expected_families):
            if family_runnable_instances[family] != {0, 1, 2}:
                raise ValueError(f"DSB runnable instance inventory mismatch for {family}")
            if family_norun_instances[family] != set(range(3, 20)):
                raise ValueError(f"DSB non-runnable instance inventory mismatch for {family}")
        if len(runnable_infos) != contract.query_count:
            raise ValueError(
                f"DSB runnable SQL count mismatch: expected={contract.query_count} actual={len(runnable_infos)}"
            )
        if len(norun_infos) != contract.norun_count:
            raise ValueError(
                f"DSB non-runnable SQL count mismatch: expected={contract.norun_count} actual={len(norun_infos)}"
            )
        if schema_info is None:
            raise ValueError("DSB archive is missing root schema.sql")
        _verify_dsb_declared_inventory(runnable_infos, norun_infos, schema_info, contract)

        archive_schema = _read_zip_member(archive, schema_info, contract.max_schema_bytes, label="DSB schema")
        if archive_schema != schema_raw or hashlib.sha256(archive_schema).hexdigest() != contract.schema_sha256:
            raise ValueError("DSB archive schema bytes differ from the tracked schema")

        loaded: dict[str, tuple[str, str, str]] = {}
        actual_total = 0
        for info, query_name in runnable_infos:
            member_raw = _read_zip_member(archive, info, contract.max_member_bytes, label="DSB SQL")
            query_sql = _decode_utf8_round_trip(member_raw, info.filename)
            loaded[query_name] = (info.filename, query_sql, hashlib.sha256(member_raw).hexdigest())
            actual_total += len(member_raw)
        if actual_total != contract.total_query_bytes:
            raise ValueError(
                f"DSB archive actual runnable SQL size mismatch: expected={contract.total_query_bytes} "
                f"actual={actual_total}"
            )
        return loaded


def _load_stack_archive(
    archive_path: Path,
    *,
    schema_raw: bytes,
    contract: _StackArchiveContract,
) -> dict[str, tuple[str, str, str]]:

    raw_archive = _read_regular_bytes(archive_path, max_bytes=contract.max_archive_bytes)
    archive_sha256 = hashlib.sha256(raw_archive).hexdigest()
    if archive_sha256 != contract.raw_sha256:
        raise ValueError(
            f"Stack archive raw SHA-256 mismatch: expected={contract.raw_sha256} actual={archive_sha256}"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_archive), mode="r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid Stack ZIP archive: {exc}") from exc

    with archive:
        if archive.comment:
            raise ValueError("Stack ZIP archive comments are not allowed")
        infos = archive.infolist()
        if len(infos) > contract.max_entries:
            raise ValueError(f"Stack archive entry limit exceeded: {len(infos)} > {contract.max_entries}")
        if len(infos) != contract.entry_count:
            raise ValueError(
                f"Stack archive entry count mismatch: expected={contract.entry_count} actual={len(infos)}"
            )

        seen_members: set[str] = set()
        seen_queries: set[str] = set()
        directories: list[str] = []
        query_infos: list[tuple[zipfile.ZipInfo, str]] = []
        schema_info: zipfile.ZipInfo | None = None
        declared_total = 0
        declared_query_total = 0
        declared_query_max = 0
        observed_query_ratio = 0.0

        for info in infos:
            member_name = info.filename
            _validate_member_path(member_name, label="Stack")
            if member_name in seen_members:
                raise ValueError(f"Stack archive contains duplicate member: {member_name}")
            seen_members.add(member_name)
            if info.flag_bits & 0x1:
                raise ValueError(f"Stack archive contains encrypted member: {member_name}")
            if info.create_system != 3:
                raise ValueError(f"Stack archive member lacks Unix file type metadata: {member_name}")

            mode = info.external_attr >> 16
            member_type = stat.S_IFMT(mode)
            if info.is_dir():
                if member_type != stat.S_IFDIR or _STACK_DIRECTORY_RE.fullmatch(member_name) is None:
                    raise ValueError(f"Stack archive contains invalid directory entry: {member_name}")
                if info.file_size != 0 or info.compress_size != 0 or info.compress_type != zipfile.ZIP_STORED:
                    raise ValueError(f"Stack archive directory carries data: {member_name}")
                directories.append(member_name)
                continue

            if member_type != stat.S_IFREG:
                raise ValueError(f"Stack archive contains non-regular member: {member_name}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"Stack archive member uses unsupported compression: {member_name}")
            member_limit = contract.max_schema_bytes if member_name == "schema.sql" else contract.max_member_bytes
            if info.file_size <= 0 or info.file_size > member_limit:
                raise ValueError(f"Stack archive member size is outside bounds: {member_name} ({info.file_size})")
            if info.compress_size <= 0:
                raise ValueError(f"Stack archive member has invalid compressed size: {member_name}")
            ratio = info.file_size / info.compress_size
            if ratio > contract.max_compression_ratio_limit:
                raise ValueError(f"Stack archive member compression ratio is outside bounds: {member_name} ({ratio})")
            declared_total += info.file_size
            if declared_total > contract.max_total_member_bytes:
                raise ValueError("Stack archive total member size exceeds bound")

            if member_name == "schema.sql":
                if schema_info is not None:
                    raise ValueError("Stack archive contains duplicate root schema")
                schema_info = info
                continue

            match = _STACK_MEMBER_RE.fullmatch(member_name)
            if match is None or len(PurePosixPath(member_name).parts) != 2:
                raise ValueError(f"Stack archive contains invalid SQL member path: {member_name}")
            query_name = "STACK_" + PurePosixPath(member_name).stem.upper()
            if query_name in seen_queries:
                raise ValueError(f"Stack archive contains duplicate normalized query ID: {query_name}")
            seen_queries.add(query_name)
            declared_query_total += info.file_size
            declared_query_max = max(declared_query_max, info.file_size)
            observed_query_ratio = max(observed_query_ratio, ratio)
            query_infos.append((info, query_name))

        if tuple(sorted(directories)) != tuple(sorted(contract.directories)):
            raise ValueError("Stack archive directory inventory mismatch")
        if len(directories) != contract.directory_count:
            raise ValueError(
                f"Stack archive directory count mismatch: expected={contract.directory_count} actual={len(directories)}"
            )
        if len(query_infos) != contract.query_count:
            raise ValueError(
                f"Stack archive SQL member count mismatch: expected={contract.query_count} actual={len(query_infos)}"
            )
        if schema_info is None:
            raise ValueError("Stack archive is missing root schema.sql")
        inventory_checks = {
            "query total size": (declared_query_total, contract.total_query_bytes),
            "query maximum size": (declared_query_max, contract.max_query_bytes),
            "query maximum compression ratio": (observed_query_ratio, contract.max_query_compression_ratio),
            "schema size": (schema_info.file_size, contract.schema_bytes),
            "schema compression ratio": (
                schema_info.file_size / schema_info.compress_size,
                contract.schema_compression_ratio,
            ),
        }
        mismatches = [name for name, (actual, expected) in inventory_checks.items() if actual != expected]
        if mismatches:
            raise ValueError(f"Stack archive declared inventory mismatch: {', '.join(mismatches)}")

        archive_schema = _read_zip_member(archive, schema_info, contract.max_schema_bytes, label="Stack schema")
        if archive_schema != schema_raw or hashlib.sha256(archive_schema).hexdigest() != contract.schema_sha256:
            raise ValueError("Stack archive schema bytes differ from the tracked schema")

        loaded: dict[str, tuple[str, str, str]] = {}
        actual_total = 0
        for info, query_name in query_infos:
            member_raw = _read_zip_member(archive, info, contract.max_member_bytes, label="Stack SQL")
            query_sql = _decode_utf8_round_trip(member_raw, info.filename)
            loaded[query_name] = (info.filename, query_sql, hashlib.sha256(member_raw).hexdigest())
            actual_total += len(member_raw)
        if actual_total != contract.total_query_bytes:
            raise ValueError(
                f"Stack archive actual SQL size mismatch: expected={contract.total_query_bytes} actual={actual_total}"
            )
        return loaded


def _verify_dsb_declared_inventory(
    runnable_infos: list[tuple[zipfile.ZipInfo, str]],
    norun_infos: list[zipfile.ZipInfo],
    schema_info: zipfile.ZipInfo,
    contract: _DsbArchiveContract,
) -> None:
    def inventory(infos: list[zipfile.ZipInfo]) -> tuple[int, int, float]:
        return (
            sum(info.file_size for info in infos),
            max(info.file_size for info in infos),
            max(info.file_size / info.compress_size for info in infos),
        )

    runnable_total, runnable_max, runnable_ratio = inventory([info for info, _ in runnable_infos])
    norun_total, norun_max, norun_ratio = inventory(norun_infos)
    checks = {
        "runnable total size": (runnable_total, contract.total_query_bytes),
        "runnable maximum size": (runnable_max, contract.max_query_bytes),
        "runnable maximum compression ratio": (runnable_ratio, contract.max_query_compression_ratio),
        "non-runnable total size": (norun_total, contract.total_norun_bytes),
        "non-runnable maximum size": (norun_max, contract.max_norun_bytes),
        "non-runnable maximum compression ratio": (norun_ratio, contract.max_norun_compression_ratio),
        "schema size": (schema_info.file_size, contract.schema_bytes),
        "schema compression ratio": (
            schema_info.file_size / schema_info.compress_size,
            contract.schema_compression_ratio,
        ),
    }
    mismatches = [name for name, (actual, expected) in checks.items() if actual != expected]
    if mismatches:
        raise ValueError(f"DSB archive declared inventory mismatch: {', '.join(mismatches)}")


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    max_bytes: int,
    *,
    label: str,
) -> bytes:
    try:
        with archive.open(info, mode="r") as member_file:
            member_raw = member_file.read(max_bytes + 1)
    except (RuntimeError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Could not read {label} member {info.filename}: {exc}") from exc
    if len(member_raw) != info.file_size:
        raise ValueError(
            f"{label} member declared/actual size mismatch: "
            f"{info.filename} ({info.file_size} != {len(member_raw)})"
        )
    return member_raw


def _validate_member_path(member_name: str, *, label: str = "CEB") -> None:
    if not member_name or not member_name.isascii():
        raise ValueError(f"{label} archive member path must be nonempty ASCII: {member_name!r}")
    if len(member_name.encode("ascii")) > _MAX_MEMBER_NAME_BYTES:
        raise ValueError(f"{label} archive member path is too long: {member_name!r}")
    if "\\" in member_name or member_name.startswith("/"):
        raise ValueError(f"{label} archive contains unsafe member path: {member_name}")
    raw_parts = member_name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"{label} archive contains unsafe member path: {member_name}")
    path = PurePosixPath(member_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} archive contains unsafe member path: {member_name}")


def _verify_schema(
    schema_path: Path,
    *,
    expected_sha256: str = IMDB_SCHEMA_SHA256,
    source_path: str = IMDB_SCHEMA_SOURCE_PATH,
) -> bytes:
    raw = _read_regular_bytes(schema_path, max_bytes=_MAX_SCHEMA_BYTES)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Schema raw SHA-256 mismatch: expected={expected_sha256} actual={actual}")
    _decode_utf8_round_trip(raw, source_path)
    return raw


def _schema_resolution_payload(
    relation_columns: dict[str, frozenset[str]],
) -> dict[str, Any]:
    canonical_columns = {
        relation: sorted(columns)
        for relation, columns in sorted(relation_columns.items())
    }
    return {
        "kind": "tracked_schema_unique_owner",
        "relation_count": len(canonical_columns),
        "column_count": sum(len(columns) for columns in canonical_columns.values()),
        "catalog_sha256": canonical_json_sha256(canonical_columns),
    }


def _read_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Could not open canonical workload source {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Canonical workload source is not a regular file: {path}")
        if metadata.st_size < 0 or metadata.st_size > max_bytes:
            raise ValueError(f"Canonical workload source size is outside bounds: {path} ({metadata.st_size})")
        with os.fdopen(descriptor, "rb", closefd=False) as file_obj:
            raw = file_obj.read(max_bytes + 1)
        if len(raw) != metadata.st_size:
            raise ValueError(
                f"Canonical workload source changed while reading: {path} ({metadata.st_size} != {len(raw)})"
            )
        return raw
    finally:
        os.close(descriptor)


def _decode_utf8_round_trip(raw: bytes, label: str) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Canonical workload source is not strict UTF-8: {label}") from exc
    if text.encode("utf-8") != raw:
        raise ValueError(f"Canonical workload source fails UTF-8 byte round trip: {label}")
    return text
