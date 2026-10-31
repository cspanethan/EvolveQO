"""Run hint_search on one workload query or SQL file."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, replace
from pathlib import Path

from genetic_lqo.core.config import load_config, validate_config
from genetic_lqo.core.profiles import AVAILABLE_PROFILES, primary_method_profile_for_workload
from genetic_lqo.core.types import GeneticSearchConfig
from genetic_lqo.execution.executor import PostgresConnectionConfig, PostgresGeneticExecutor
from genetic_lqo.search.optimizer import EXTERNAL_SQL_DIAGNOSTIC_ORIGIN, run_genetic_lqo
from genetic_lqo.workload.canonical import CanonicalWorkloadContext
from genetic_lqo.workload.task_contract import get_task_contract_for_workload
from genetic_lqo.workload.workloads import AVAILABLE_WORKLOAD_SETS, get_workload_set, infer_implicit_workload_set

DEFAULT_PROFILE = "method"


@dataclass(frozen=True)
class LoadedQuery:
    query_sql: str
    query_name: str
    database_name: str
    canonical_workload_context: CanonicalWorkloadContext | None = None
    query_origin: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--method", choices=("hint_search",))
    configuration = parser.add_mutually_exclusive_group()
    configuration.add_argument("--profile", choices=AVAILABLE_PROFILES, default=None,
                               help=f"Tracked profile (default: {DEFAULT_PROFILE}).")
    configuration.add_argument("--config", default="", help="TOML overlay on the method profile.")
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--workload-name")
    query.add_argument("--query-sql-file")
    parser.add_argument("--workload-set", choices=AVAILABLE_WORKLOAD_SETS, default="",
                        help="Canonical workload edition. Required for CEB queries.")
    parser.add_argument("--time-budget", type=float, help="T_max in seconds (planning, LLM calls and evaluations).")
    parser.add_argument("--cutoff-factor", type=float, help="kappa: cut evaluations off at kappa x best latency.")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--native-seconds", type=float,
                        help="Use this default-plan latency (e.g. from an earlier run) as the starting best.")
    parser.add_argument("--programs", nargs="+", choices=("card", "hint"),
                        help="Search programs to run (default both); one program is the single-mechanism ablation.")
    parser.add_argument("--llm", action=argparse.BooleanOptionalAction, default=None,
                        help="--no-llm runs the default neighborhoods only (mechanical control).")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--db-name", default="")
    parser.add_argument("--verify-db", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    loaded = _load_query_input(args)
    config = _apply_cli_overrides(_load_cli_config(args), args)
    db_name = args.db_name or loaded.database_name
    database = replace(config.database, dbname=db_name)
    if loaded.canonical_workload_context is not None and not os.getenv("DB_USER") and not args.config:
        database = replace(database, user=loaded.canonical_workload_context.payload()["database"]["user"])
    config = replace(config, database=database)
    connection = PostgresConnectionConfig.from_config(config.database, db_name)
    if args.verify_db:
        info = PostgresGeneticExecutor(connection).verify_pg_hint_plan()
        print(info)
        if not info["pg_hint_plan_loaded"]:
            raise SystemExit("pg_hint_plan must be installed and present in shared_preload_libraries")
        return
    if not args.output_dir:
        from genetic_lqo.core.artifacts import create_experiment_directory
        directory = create_experiment_directory(Path(config.output.output_dir).resolve())
        config = replace(config, output=replace(config.output, output_dir=str(directory)))
    result = run_genetic_lqo(loaded.query_sql, loaded.query_name, connection, config,
                             canonical_workload_context=loaded.canonical_workload_context,
                             query_origin=loaded.query_origin)
    best = result.summary["best_so_far"]
    print(f"search_status={result.summary['status']} stop_reason={result.summary['stop_reason']}")
    print(f"native_seconds={result.summary['native']['seconds']:.6f} best_seconds={best['seconds']:.6f}")
    print(f"best_hints={' '.join(best['hints'])}")
    for label, path in result.output_files.items():
        print(f"{label}_json={path}")


def _load_cli_config(args: argparse.Namespace) -> GeneticSearchConfig:
    if args.config:
        return load_config(args.config)
    if args.profile is None and args.workload_set:
        return load_config(profile=primary_method_profile_for_workload(args.workload_set))
    return load_config(profile=args.profile or DEFAULT_PROFILE)


def _apply_cli_overrides(config: GeneticSearchConfig, args: argparse.Namespace) -> GeneticSearchConfig:
    search = config.search
    for name, value in (("time_budget_seconds", args.time_budget), ("cutoff_factor", args.cutoff_factor),
                        ("seed", args.seed), ("use_llm", args.llm),
                        ("programs", tuple(args.programs) if args.programs else None),
                        ("native_seconds", args.native_seconds)):
        if value is not None:
            search = replace(search, **{name: value})
    output = replace(config.output, output_dir=args.output_dir) if args.output_dir else config.output
    return validate_config(replace(config, search=search, output=output))


def _load_query_input(args: argparse.Namespace) -> LoadedQuery:
    if args.query_sql_file:
        if args.workload_set:
            raise ValueError("--workload-set cannot be combined with --query-sql-file")
        path = Path(args.query_sql_file)
        return LoadedQuery(path.read_text(encoding="utf-8"), path.stem, args.db_name or "imdb",
                           query_origin=EXTERNAL_SQL_DIAGNOSTIC_ORIGIN)
    workload_set = get_workload_set(args.workload_set or infer_implicit_workload_set(args.workload_name))
    definition = workload_set.queries.get(args.workload_name)
    if definition is None:
        raise ValueError(f"Query {args.workload_name!r} does not belong to workload {workload_set.name!r}")
    context = None
    if args.workload_set or workload_set.name != "JOB":
        context = CanonicalWorkloadContext.from_tracked(workload_set, get_task_contract_for_workload(workload_set),
                                                        definition)
    return LoadedQuery(definition.query_sql, definition.name, definition.db, canonical_workload_context=context)


if __name__ == "__main__":
    main()
