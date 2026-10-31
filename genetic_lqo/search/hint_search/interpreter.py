from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from string import Template

from genetic_lqo.core.artifacts import write_json
from genetic_lqo.search.hint_search.models import CARD, Config, HintGrammar
from genetic_lqo.search.hint_search.observations import plan_text
from genetic_lqo.search.hint_search.recipe import Recipe, default_recipe, parse_recipe

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "interpreter.md"
PROGRAM_NAMES = {CARD: "cardinality", "hint": "hint"}
_HINTS = {"type": "array", "items": {"type": "string"}}
_RANGES = {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "number"}}}
RECIPE_SCHEMAS = {
    "hint": {"name": "hint_recipe", "strict": False, "schema": {
        "type": "object", "required": ["diagnosis", "neighborhood", "seeds"],
        "properties": {"diagnosis": {"type": "string"}, "seeds": {"type": "array", "items": _HINTS},
                       "neighborhood": {"type": "object", "properties": {
                           "access_path": _HINTS, "join_method": _HINTS, "join_order": _HINTS,
                           "coupled": {"type": "array", "items": _HINTS}}}}}},
    CARD: {"name": "cardinality_recipe", "strict": False, "schema": {
        "type": "object", "required": ["diagnosis", "neighborhood", "seeds"],
        "properties": {"diagnosis": {"type": "string"}, "neighborhood": {"type": "array", "items": _RANGES},
                       "seeds": {"type": "array", "items": {
                           "type": "object", "additionalProperties": {"type": "number"}}}}}},
}


def prompt_sections(path: Path = PROMPT_PATH) -> dict[str, str]:
    parts = re.split(r"^<!-- ([\w.]+) -->\n", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return {name: body.strip() for name, body in zip(parts[1::2], parts[2::2])}


def render_messages(program: str, stage: str, values: dict) -> list[dict[str, str]]:
    sections = prompt_sections()
    system = sections["system"] + "\n\n" + sections[f"format.{program}"]
    user = Template(sections[stage]).substitute(program=PROGRAM_NAMES[program], **values)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def describe_config(config: Config) -> str:
    parts = [value for _, value in config.hint]
    parts += [f"scale {' '.join(scope)} by 10^{value:g}" for scope, value in config.card]
    return "; ".join(parts) if parts else "none (native optimizer)"


def seconds_text(seconds: float, censored: bool = False) -> str:
    text = f"{seconds * 1000:.1f} ms" if seconds < 1 else f"{seconds:.2f} s"
    return f"more than {text} (cut off)" if censored else text


class Interpreter:
    def __init__(self, proposer, grammar: HintGrammar, *, sql: str, tables: str, output_dir: Path):
        self.proposer, self.grammar = proposer, grammar
        self.static = {"sql": sql.strip(), "tables": tables}
        self.output_dir = Path(output_dir)
        self.calls = 0
        self._numbers = itertools.count(1)

    def initial_values(self, seconds: float, explain_json) -> dict:
        return {**self.static, "seconds": seconds_text(seconds), "plan": plan_text(explain_json)}

    def transfer_values(self, *, winner: str, before: tuple, after: tuple, receiving: str, tasks: list) -> dict:
        (old, old_seconds, old_plan, old_analyzed), (new, new_seconds, new_plan, new_analyzed) = before, after
        fixed_program = "hint" if receiving == CARD else CARD
        fixed = describe_config(Config.make(**{fixed_program: new.component(fixed_program)}))
        return {
            **self.static, "winner": PROGRAM_NAMES[winner],
            "before_seconds": seconds_text(old_seconds), "before_config": describe_config(old),
            "before_plan": plan_text(old_plan, analyze=old_analyzed),
            "after_seconds": seconds_text(new_seconds), "after_config": describe_config(new),
            "after_plan": plan_text(new_plan, analyze=new_analyzed),
            "fixed_part": "the hints" if receiving == CARD else "the cardinality scaling",
            "own_part": "cardinality scaling" if receiving == CARD else "hints",
            "fixed": fixed, "tasks": "\n".join(tasks) or "None yet.",
        }

    def request(self, program: str, stage: str, values: dict, *, base: Config, base_plan) -> Recipe:
        messages = render_messages(program, stage, values)
        if self.proposer is None:
            recipe = default_recipe(program, base=base, grammar=self.grammar, base_plan=base_plan)
        else:
            self.calls += 1
            directory = self.output_dir / "llm" / f"{next(self._numbers):03d}"
            try:
                content = self.proposer.for_call(directory)(messages, schema=RECIPE_SCHEMAS[program])
                recipe = parse_recipe(program, content, base=base, grammar=self.grammar, base_plan=base_plan)
            except Exception as exc:  # noqa: BLE001
                recipe = default_recipe(program, base=base, grammar=self.grammar, base_plan=base_plan)
                recipe.rejected.append({"entry": "call", "reason": f"{type(exc).__name__}: {exc}"})
            write_json(directory / "recipe.json", {
                "program": program, "stage": stage, "default": recipe.default, "diagnosis": recipe.diagnosis,
                "neighborhood": recipe.neighborhood(), "seeds": [_seed_text(program, s) for s in recipe.seeds],
                "rejected": recipe.rejected})
        return recipe


def _seed_text(program: str, seed: dict) -> object:
    if program == CARD:
        return {" ".join(scope): value for scope, value in seed.items()}
    return list(seed.values())


def recipe_summary(recipe: Recipe) -> str:
    return json.dumps(recipe.neighborhood(), ensure_ascii=False)
