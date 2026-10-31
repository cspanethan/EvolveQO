from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def stable_id(prefix: str, value: str, length: int = 12) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}:{digest}"


@dataclass(frozen=True)
class HintGene:
    hint: str
    hint_type: str = "unknown"
    target_tables: tuple[str, ...] = ()
    source: str = "candidate_provider"
    intent: str = ""
    gene_id: str = ""

    def __post_init__(self) -> None:
        if not self.gene_id:
            object.__setattr__(self, "gene_id", stable_id(self.source, self.hint))


@dataclass(frozen=True)
class EstimationError:
    aliases: tuple[str, ...]
    node_type: str
    plan_rows: float
    actual_rows: float
    ratio: float


@dataclass(frozen=True)
class RuntimeHotspot:
    aliases: tuple[str, ...]
    node_type: str
    total_time_ms: float
    actual_rows: float | None = None
    loops: int | None = None


@dataclass(frozen=True)
class JoinMethodObservation:
    aliases: tuple[str, ...]
    node_type: str
    join_type: str = ""
    child_aliases: tuple[tuple[str, ...], ...] = ()


@dataclass
class PlanSummary:
    plan_digest: str
    shape: str
    aliases: tuple[str, ...] = ()
    join_alias_sets: tuple[tuple[str, ...], ...] = ()
    join_order_signature: str = ""
    join_methods: tuple[JoinMethodObservation, ...] = ()
    table_accesses: tuple[dict[str, Any], ...] = ()
    estimation_errors: tuple[EstimationError, ...] = ()
    cardinality_observations: tuple[EstimationError, ...] = ()
    runtime_hotspots: tuple[RuntimeHotspot, ...] = ()
    total_cost: float | None = None
    execution_time_seconds: float | None = None
    planning_time_seconds: float | None = None


@dataclass(frozen=True)
class HintDiagnostics:
    status: str
    requested_hints: tuple[str, ...] = ()
    used_hints: tuple[str, ...] = ()
    not_used_hints: tuple[str, ...] = ()
    duplicate_hints: tuple[str, ...] = ()
    error_hints: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()


@dataclass
class ExecutionResult:
    sql: str
    hints: tuple[str, ...]
    success: bool
    analyze: bool
    plan_digest: str | None = None
    explain_json: Any | None = None
    plan_summary: PlanSummary | None = None
    hint_diagnostics: HintDiagnostics | None = None
    execution_time_seconds: float | None = None
    planning_time_seconds: float | None = None
    elapsed_wall_seconds: float | None = None
    timed_out: bool = False
    timeout_seconds: float | None = None
    error: str = ""


@dataclass
class DatabaseConfig:
    host: str = ""
    port: int | None = None
    dbname: str = ""
    user: str = ""
    password: str = ""


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    reasoning_effort: str | None = None
    timeout_seconds: float = 300.0
    max_tokens: int = 4096
    stream: bool = True
    retry_max_attempts: int = 2
    temperature: float | None = None


@dataclass
class SearchConfig:
    method: str = "hint_search"
    time_budget_seconds: float = 600.0
    cutoff_factor: float = 2.0
    seed: int = 71
    use_llm: bool = True
    programs: tuple[str, ...] = ("card", "hint")
    native_seconds: float | None = None
    baseline_timeout_seconds: float = 90.0
    explain_timeout_seconds: float = 5.0


@dataclass
class PlanCacheConfig:
    enabled: bool = True
    dbname: str = ""
    schema: str = "lqo_cache_v2"


@dataclass
class OutputConfig:
    output_dir: str = "runs/hint_search"


@dataclass
class GeneticSearchConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    cache: PlanCacheConfig = field(default_factory=PlanCacheConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def hint_key(hints: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(hint.strip() for hint in hints if hint and hint.strip()))
