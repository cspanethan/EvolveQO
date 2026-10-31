from __future__ import annotations

import codecs
import hashlib
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from genetic_lqo.core.artifacts import canonical_json_sha256

from .workloads import (
    REPOSITORY_ROOT,
    STACK_UPSTREAM_REPOSITORY,
    STACK_UPSTREAM_REVISION,
    UpstreamGitSource,
    WorkloadSet,
    get_workload_set,
)

TASK_CONTRACT_SCHEMA_VERSION = 2
_MAX_TASK_BYTES = 1024 * 1024
_SAFE_QUERY_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*_[A-Z0-9]+\Z", re.ASCII)


class TaskProvenance(str, Enum):

    UPSTREAM_DEFINED = "upstream_defined"
    PROJECT_DEFINED = "project_defined"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class TaskContract:
    workload_set: str
    provenance: TaskProvenance
    source_path: str
    raw_text: str
    raw_sha256: str
    ends_with_newline: bool
    ordered_occurrences: tuple[str, ...]
    ordered_occurrences_sha256: str
    ordered_unique_names: tuple[str, ...]
    ordered_unique_names_sha256: str
    weights: dict[str, int]
    weights_sha256: str
    occurrence_count: int
    unique_count: int
    source_authority: UpstreamGitSource | None = None

    def identity_payload(self) -> dict[str, Any]:

        ordered_weights = {name: self.weights[name] for name in self.ordered_unique_names}
        payload = {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "workload_set": self.workload_set,
            "provenance": self.provenance.value,
            "source": {
                "repo_relative_path": self.source_path,
                "raw_sha256": self.raw_sha256,
                "raw_text": self.raw_text,
                "ends_with_newline": self.ends_with_newline,
            },
            "occurrence_count": self.occurrence_count,
            "unique_count": self.unique_count,
            "ordered_occurrences": list(self.ordered_occurrences),
            "ordered_occurrences_sha256": self.ordered_occurrences_sha256,
            "ordered_unique_names": list(self.ordered_unique_names),
            "ordered_unique_names_sha256": self.ordered_unique_names_sha256,
            "weights": ordered_weights,
            "weights_sha256": self.weights_sha256,
        }
        if self.source_authority is not None:
            if self.source_authority.raw_sha256 != self.raw_sha256:
                raise ValueError("Task upstream authority hash differs from its tracked source hash")
            payload["source"]["upstream_authority"] = self.source_authority.identity_payload()
        return payload


@dataclass(frozen=True)
class _KnownTaskContract:
    provenance: TaskProvenance
    source_path: str
    raw_sha256: str
    ends_with_newline: bool
    occurrence_count: int
    unique_count: int
    ordered_occurrences_sha256: str
    ordered_unique_names_sha256: str
    weights_sha256: str
    source_authority: UpstreamGitSource | None = None


_STACK_TASK_CONTRACT = _KnownTaskContract(
    provenance=TaskProvenance.UPSTREAM_DEFINED,
    source_path="tasks/so_tasks.txt",
    raw_sha256="f5e1d94ee70587c296c7a33e5177d9ce1cf1285159326f258759a2205166e75d",
    ends_with_newline=False,
    occurrence_count=200,
    unique_count=200,
    ordered_occurrences_sha256="fabe5f383067bd10abc33093a169a24ec733431bb82721f199f8fd7e8f955f52",
    ordered_unique_names_sha256="fabe5f383067bd10abc33093a169a24ec733431bb82721f199f8fd7e8f955f52",
    weights_sha256="5fb88c5b27fc5baf5c9902934a494bd49bde4c6e471aa705d650f69028498fda",
    source_authority=UpstreamGitSource(
        repository_url=STACK_UPSTREAM_REPOSITORY,
        revision=STACK_UPSTREAM_REVISION,
        repo_relative_path="tasks/so_tasks.txt",
        raw_sha256="f5e1d94ee70587c296c7a33e5177d9ce1cf1285159326f258759a2205166e75d",
    ),
)


_KNOWN_TASK_CONTRACTS = {
    "JOB": _KnownTaskContract(
        provenance=TaskProvenance.UPSTREAM_DEFINED,
        source_path="tasks/job_tasks.txt",
        raw_sha256="fad1b309c77efeeaecca87768482e022d977c4aa1b88f57a2b62c2664dde2273",
        ends_with_newline=False,
        occurrence_count=113,
        unique_count=113,
        ordered_occurrences_sha256="dcc9097de3d1bff3b03139559f3f369ae333ae5082857919a8ad81617a57d3cc",
        ordered_unique_names_sha256="dcc9097de3d1bff3b03139559f3f369ae333ae5082857919a8ad81617a57d3cc",
        weights_sha256="4e1c9b22376e009e27c36454df34e3d7f8fd02e922205c30622e49b9aa568713",
    ),
    "CEB_3K": _KnownTaskContract(
        provenance=TaskProvenance.UPSTREAM_DEFINED,
        source_path="tasks/ceb_tasks.txt",
        raw_sha256="7812d753ba9c91540698a834275db4ffada38b324d5ab3f74f4c7621788e8966",
        ends_with_newline=False,
        occurrence_count=300,
        unique_count=233,
        ordered_occurrences_sha256="97e794c18fec858164118a73cdd2c88f144679f153c2aecdb5ea4fd26ea40712",
        ordered_unique_names_sha256="857145168f1424d608cac09f748061f046532a43d4a52f705abe35cc2ce617b4",
        weights_sha256="dd95490c44e7b72d46e824e22acb0638b465134d4f5516416990aa375877ba92",
    ),
    "DSB": _KnownTaskContract(
        provenance=TaskProvenance.PROJECT_DEFINED,
        source_path="tasks/dsb_tasks.txt",
        raw_sha256="07fe105dd727ee4aa39bc57f7219b797f9907f8f93d59e518dfc313ab6f2faf7",
        ends_with_newline=True,
        occurrence_count=90,
        unique_count=90,
        ordered_occurrences_sha256="02c2fc1cbf57fd23765a984e3f4603fecd48b5f560c5862eb46d4a1bbb532820",
        ordered_unique_names_sha256="02c2fc1cbf57fd23765a984e3f4603fecd48b5f560c5862eb46d4a1bbb532820",
        weights_sha256="169e044e243c27e16725b7124d0392df87440fcb61c249520f5a77ace483490f",
    ),
    "STACK_SO_PAST": _STACK_TASK_CONTRACT,
    "STACK_SO_FUTURE": _STACK_TASK_CONTRACT,
    "STACK_SO_SHIFTED": _STACK_TASK_CONTRACT,
}


def get_task_contract(workload_set: str) -> TaskContract:

    normalized = workload_set.strip().upper()
    return get_task_contract_for_workload(get_workload_set(normalized))


def get_task_contract_for_workload(workload: WorkloadSet) -> TaskContract:

    if not isinstance(workload, WorkloadSet):
        raise TypeError("workload must be a WorkloadSet")
    normalized = workload.name
    known = _KNOWN_TASK_CONTRACTS.get(normalized)
    if known is None:
        raise ValueError(f"No tracked task contract for workload set {workload.name!r}")
    raw = _read_task_bytes(REPOSITORY_ROOT / known.source_path)
    return parse_task_contract(
        raw,
        workload,
        source_path=known.source_path,
        require_known_identity=True,
    )


def parse_task_contract(
    raw: bytes,
    workload_set: WorkloadSet | str,
    *,
    source_path: str = "",
    require_known_identity: bool = True,
) -> TaskContract:

    if type(raw) is not bytes:
        raise TypeError("Task contract raw input must be bytes")
    if not raw:
        raise ValueError("Task contract is empty")
    if len(raw) > _MAX_TASK_BYTES:
        raise ValueError(f"Task contract exceeds size limit: {len(raw)} > {_MAX_TASK_BYTES}")
    if raw.startswith(codecs.BOM_UTF8):
        raise ValueError("Task contract must not contain a UTF-8 BOM")
    if b"\r" in raw:
        raise ValueError("Task contract must use LF line endings")
    try:
        raw_text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Task contract is not strict UTF-8") from exc
    if raw_text.encode("utf-8") != raw:
        raise ValueError("Task contract fails UTF-8 byte round trip")

    ends_with_newline = raw_text.endswith("\n")
    body = raw_text[:-1] if ends_with_newline else raw_text
    if not body:
        raise ValueError("Task contract has no query occurrences")
    lines = body.split("\n")
    if any(not line for line in lines):
        raise ValueError("Task contract contains a blank line")
    loaded_workload = get_workload_set(workload_set) if isinstance(workload_set, str) else workload_set
    query_name_re = (
        _SAFE_QUERY_NAME_RE
        if loaded_workload.query_name_pattern == _SAFE_QUERY_NAME_RE.pattern
        else re.compile(loaded_workload.query_name_pattern, re.ASCII)
    )
    invalid = [line for line in lines if query_name_re.fullmatch(line) is None]
    if invalid:
        raise ValueError(f"Task contract contains unsafe query name: {invalid[0]!r}")

    unknown = sorted(set(lines) - set(loaded_workload.queries))
    if unknown:
        raise ValueError(f"Task contract contains unknown {loaded_workload.name} queries: {', '.join(unknown)}")

    ordered_occurrences = tuple(lines)
    ordered_unique_names = tuple(dict.fromkeys(ordered_occurrences))
    counts = Counter(ordered_occurrences)
    weights = {name: counts[name] for name in ordered_unique_names}
    known = _KNOWN_TASK_CONTRACTS.get(loaded_workload.name)
    contract = TaskContract(
        workload_set=loaded_workload.name,
        provenance=known.provenance if require_known_identity and known is not None else TaskProvenance.DIAGNOSTIC,
        source_path=source_path,
        raw_text=raw_text,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        ends_with_newline=ends_with_newline,
        ordered_occurrences=ordered_occurrences,
        ordered_occurrences_sha256=canonical_json_sha256(list(ordered_occurrences)),
        ordered_unique_names=ordered_unique_names,
        ordered_unique_names_sha256=canonical_json_sha256(list(ordered_unique_names)),
        weights=weights,
        weights_sha256=canonical_json_sha256(weights),
        occurrence_count=len(ordered_occurrences),
        unique_count=len(ordered_unique_names),
        source_authority=(known.source_authority if require_known_identity and known is not None else None),
    )
    if require_known_identity:
        if known is None:
            raise ValueError(f"No tracked task identity for workload set {loaded_workload.name!r}")
        _verify_known_identity(contract, known)
    return contract


def _verify_known_identity(contract: TaskContract, known: _KnownTaskContract) -> None:
    checks = {
        "provenance": (contract.provenance, known.provenance),
        "source_path": (contract.source_path, known.source_path),
        "raw_sha256": (contract.raw_sha256, known.raw_sha256),
        "ends_with_newline": (contract.ends_with_newline, known.ends_with_newline),
        "occurrence_count": (contract.occurrence_count, known.occurrence_count),
        "unique_count": (contract.unique_count, known.unique_count),
        "ordered_occurrences_sha256": (
            contract.ordered_occurrences_sha256,
            known.ordered_occurrences_sha256,
        ),
        "ordered_unique_names_sha256": (
            contract.ordered_unique_names_sha256,
            known.ordered_unique_names_sha256,
        ),
        "weights_sha256": (contract.weights_sha256, known.weights_sha256),
    }
    mismatches = [name for name, (actual, expected) in checks.items() if actual != expected]
    if mismatches:
        details = ", ".join(
            f"{name}: expected={checks[name][1]!r} actual={checks[name][0]!r}" for name in mismatches
        )
        raise ValueError(f"Tracked {contract.workload_set} task identity mismatch: {details}")


def _read_task_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Could not open tracked task source {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Tracked task source is not a regular file: {path}")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_TASK_BYTES:
            raise ValueError(f"Tracked task source size is outside bounds: {path} ({metadata.st_size})")
        with os.fdopen(descriptor, "rb", closefd=False) as file_obj:
            raw = file_obj.read(_MAX_TASK_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise ValueError(f"Tracked task source changed while reading: {path}")
        return raw
    finally:
        os.close(descriptor)
