"""Render, check or try the interpreter prompt without a database.

  check                                  render every program × stage variant
  preview --query JOB_7C [--program card] [--stage transfer]
  call    --query JOB_7C [--program hint] --output-dir DIR   one real model request
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from genetic_lqo.search.hint_search.interpreter import RECIPE_SCHEMAS, Interpreter, render_messages
from genetic_lqo.search.hint_search.models import CARD, HINT, PROGRAMS, Config, HintGrammar

_PLAN_PLACEHOLDER = "(the executed plan with estimated and actual rows appears here at run time)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    for name in ("preview", "call"):
        command = commands.add_parser(name)
        command.add_argument("--query", required=True, help="Workload query name, e.g. JOB_7C")
        command.add_argument("--workload-set", help="Workload edition when it cannot be inferred from the name")
        command.add_argument("--program", choices=PROGRAMS, default=HINT)
        command.add_argument("--stage", choices=("initial", "transfer"), default="initial")
        if name == "call":
            command.add_argument("--output-dir", type=Path, required=True)
            command.add_argument("--config", type=Path, help="TOML overlay for the model endpoint")
    return parser


def example_values(sql: str, aliases: list[str], program: str, stage: str) -> dict:
    tables = "\n".join(f"- {alias}" for alias in aliases)
    interpreter = Interpreter(None, HintGrammar(aliases, {}), sql=sql, tables=tables, output_dir=Path("."))
    if stage == "initial":
        return {**interpreter.static, "seconds": "1.20 s", "plan": _PLAN_PLACEHOLDER}
    pair = tuple(aliases[:2])
    before = Config()
    after = (Config.make(hint={("join", *pair): f"NestLoop({' '.join(pair)})"}) if program == CARD
             else Config.make(card={pair: -2.0}))
    values = interpreter.transfer_values(
        winner=HINT if program == CARD else CARD, receiving=program, tasks=[],
        before=(before, 1.2, None, True), after=(after, 0.3, None, True))
    return {**values, "before_plan": _PLAN_PLACEHOLDER, "after_plan": _PLAN_PLACEHOLDER}


def _query(args) -> tuple[str, list[str]]:
    from genetic_lqo.planning.plan import extract_alias_map
    from genetic_lqo.workload.workloads import get_workload_set, infer_implicit_workload_set

    workload = get_workload_set(args.workload_set or infer_implicit_workload_set(args.query))
    definition = workload.queries.get(args.query)
    if definition is None:
        raise SystemExit(f"Unknown query {args.query!r}")
    return definition.query_sql, list(extract_alias_map(definition.query_sql))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        sql = "SELECT count(*) FROM ta a, tb b, tc c WHERE a.id = b.a_id AND b.id = c.b_id"
        for program in PROGRAMS:
            for stage in ("initial", "transfer"):
                messages = render_messages(program, stage, example_values(sql, ["a", "b", "c"], program, stage))
                print(f"ok {program}/{stage}: {sum(len(m['content']) for m in messages)} chars")
        return 0
    sql, aliases = _query(args)
    messages = render_messages(args.program, args.stage, example_values(sql, aliases, args.program, args.stage))
    if args.command == "preview":
        for message in messages:
            print(f"===== {message['role']} =====\n{message['content']}\n")
        return 0
    from genetic_lqo.core.config import load_config
    from genetic_lqo.guidance.prompt_exchange import call_provider
    from genetic_lqo.search.hint_search.recipe import parse_recipe

    content = call_provider(load_config(args.config), messages, args.output_dir,
                            schema=RECIPE_SCHEMAS[args.program])
    recipe = parse_recipe(args.program, content, base=Config(), grammar=HintGrammar(aliases, {}), base_plan=None)
    print(json.dumps({"diagnosis": recipe.diagnosis, "neighborhood": recipe.neighborhood(),
                      "default": recipe.default, "rejected": recipe.rejected}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
