"""Run hint_search over one canonical workload set."""

from __future__ import annotations

import argparse
from pathlib import Path

from genetic_lqo.core.profiles import AVAILABLE_PROFILES, primary_method_profile_for_workload
from genetic_lqo.experiments.method_batch import METHOD_BATCH_MANIFEST_NAME, MethodBatchRequest, run_method_batch
from genetic_lqo.workload.workloads import AVAILABLE_WORKLOAD_SETS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--workload-set", choices=AVAILABLE_WORKLOAD_SETS, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--method", choices=("hint_search",), default="hint_search")
    configuration = parser.add_mutually_exclusive_group()
    configuration.add_argument("--profile", choices=AVAILABLE_PROFILES)
    configuration.add_argument("--config", type=Path, help="TOML overlay on the method profile.")
    parser.add_argument("--time-budget", type=float, help="Per-query T_max in seconds (default: profile).")
    parser.add_argument("--cutoff-factor", type=float, help="kappa (default: profile).")
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--llm", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--allow-subset", action="store_true")
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def request_from_args(args: argparse.Namespace) -> MethodBatchRequest:
    return MethodBatchRequest(
        workload_set=args.workload_set, output_dir=args.output_dir, method=args.method,
        profile_name=args.profile or primary_method_profile_for_workload(args.workload_set, args.method),
        config_path=args.config,
        seed=args.seed, use_llm=args.llm, time_budget_seconds=args.time_budget, cutoff_factor=args.cutoff_factor,
        allow_subset=args.allow_subset, workload_names=tuple(args.workload), offset=args.offset, limit=args.limit,
        dry_run=args.dry_run, resume=args.resume)


def main(argv: list[str] | None = None) -> int:
    request = request_from_args(build_parser().parse_args(argv))
    manifest = run_method_batch(request)
    print(f"batch_manifest={request.output_dir / METHOD_BATCH_MANIFEST_NAME}")
    print(f"batch_status={manifest['status']}")
    print(f"selected_cells={manifest['selection']['selected_cell_count']}")
    return 1 if manifest["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
