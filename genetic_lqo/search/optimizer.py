from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from genetic_lqo.core.artifacts import create_experiment_directory
from genetic_lqo.core.config import load_llm_api_key_from_environment, validate_config
from genetic_lqo.core.profiles import load_profile, primary_method_profile_for_workload
from genetic_lqo.core.types import GeneticSearchConfig
from genetic_lqo.execution.executor import PostgresConnectionConfig, PostgresGeneticExecutor
from genetic_lqo.execution.runtime_cache import PlanRuntimeCache
from genetic_lqo.guidance.prompt_exchange import RecordedProposer
from genetic_lqo.search.hint_search.runtime import HintSearchRuntime
from genetic_lqo.workload.canonical import CanonicalWorkloadContext

EXTERNAL_SQL_DIAGNOSTIC_ORIGIN = "external_sql_diagnostic"
PLANNER_SETTINGS = {"geqo": "off"}


@dataclass
class HintSearchResult:
    query_name: str
    query_sql: str
    config: GeneticSearchConfig
    summary: dict[str, Any]
    output_files: dict[str, str] = field(default_factory=dict)


class GeneticLQO:
    def __init__(self, query_sql: str, query_name: str, connection_config: PostgresConnectionConfig,
                 config: GeneticSearchConfig | None = None, *,
                 canonical_workload_context: CanonicalWorkloadContext | None = None,
                 query_origin: str = "") -> None:
        if query_origin not in {"", EXTERNAL_SQL_DIAGNOSTIC_ORIGIN}:
            raise ValueError(f"Unsupported query origin: {query_origin!r}")
        if canonical_workload_context is not None:
            canonical_workload_context.require_query(query_name, query_sql)
            canonical_workload_context.require_connection(connection_config.dbname, connection_config.user)
        self.query_sql, self.query_name = query_sql, query_name
        self.connection_config = connection_config
        self._allocate_output = config is None
        if config is None:
            profile = (primary_method_profile_for_workload(canonical_workload_context.workload_set)
                       if canonical_workload_context else "method")
            config = load_profile(profile).config
            config = replace(config, llm=replace(config.llm,
                                                 api_key=load_llm_api_key_from_environment(config.llm.provider)))
        self.config = validate_config(config)

    def run(self) -> HintSearchResult:
        config = self.config
        if self._allocate_output:
            directory = create_experiment_directory(Path(config.output.output_dir).resolve())
            config = replace(config, output=replace(config.output, output_dir=str(directory)))
            self.config = config
        output = Path(config.output.output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise ValueError(f"hint_search output directory must be empty: {output}")
        executor = PostgresGeneticExecutor(self.connection_config, planner_settings=PLANNER_SETTINGS)
        cache = PlanRuntimeCache.open(self.connection_config, config, PLANNER_SETTINGS,
                                      sql=self.query_sql, query_name=self.query_name)
        try:
            proposer = RecordedProposer(config) if config.search.use_llm else None
            summary = HintSearchRuntime(config, self.query_sql, self.query_name, executor, output,
                                        proposer=proposer, cache=cache).run()
        finally:
            cache.close()
        files = {name: str(output / f"{name}.json") for name in ("summary", "trace", "context")}
        return HintSearchResult(self.query_name, self.query_sql, config,
                                {"method": "hint_search", **summary}, files)


def run_genetic_lqo(query_sql: str, query_name: str, connection_config: PostgresConnectionConfig,
                    config: GeneticSearchConfig | None = None, *,
                    canonical_workload_context: CanonicalWorkloadContext | None = None,
                    query_origin: str = "") -> HintSearchResult:
    return GeneticLQO(query_sql, query_name, connection_config, config,
                      canonical_workload_context=canonical_workload_context, query_origin=query_origin).run()
