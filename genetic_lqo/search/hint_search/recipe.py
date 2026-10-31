from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass, field

from genetic_lqo.search.hint_search.models import (
    CARD,
    DEFAULT_RANGE,
    LOG_STEP,
    Config,
    HintGrammar,
    hint_slot,
    quantize,
)
from genetic_lqo.search.hint_search.observations import plan_decisions


@dataclass(frozen=True)
class Move:

    items: tuple[tuple, ...]

    @property
    def slots(self) -> tuple:
        return tuple(slot for slot, _ in self.items)

    @property
    def coupled(self) -> bool:
        return len(self.items) > 1

    def apply(self, values: dict, rng: random.Random) -> dict:
        out = dict(values)
        for slot, value in self.items:
            out[slot] = quantize(rng.uniform(*value)) if isinstance(value, tuple) else value
        return out

    def outcomes(self) -> list[dict]:
        choices = [[(slot, v) for v in _grid(value)] if isinstance(value, tuple) else [(slot, value)]
                   for slot, value in self.items]
        results = [{}]
        for options in choices:
            results = [{**partial, slot: v} for partial in results for slot, v in options]
        return results

    def describe(self) -> object:
        if all(isinstance(value, tuple) for _, value in self.items):
            return {" ".join(scope): list(bounds) for scope, bounds in self.items}
        return [value for _, value in self.items]


def _grid(bounds: tuple[float, float]) -> list[float]:
    low, high = quantize(bounds[0]), quantize(bounds[1])
    return [quantize(low + i * LOG_STEP) for i in range(round((high - low) / LOG_STEP) + 1)]


@dataclass
class Recipe:
    program: str
    base: Config
    moves: list[Move]
    seeds: list[dict]
    diagnosis: str = ""
    rejected: list[dict] = field(default_factory=list)
    default: bool = False

    def neighborhood(self) -> object:
        if self.program == CARD:
            return [move.describe() for move in self.moves]
        pools = {"access_path": [], "join_method": [], "join_order": [], "coupled": []}
        for move in self.moves:
            if move.coupled:
                pools["coupled"].append(move.describe())
            else:
                (slot, hint), = move.items
                pools[{"scan": "access_path", "join": "join_method", "order": "join_order"}[slot[0]]].append(hint)
        return pools


def add_move(moves: list[Move], move: Move) -> bool:
    if move in moves or not move.items:
        return False
    moves.append(move)
    return True


def plan_moves(program: str, explain_json, grammar: HintGrammar) -> list[Move]:
    decisions = plan_decisions(explain_json)
    if program == CARD:
        return [Move(((scope, DEFAULT_RANGE),)) for scope in decisions["scopes"] if _valid_scope(grammar, scope)]
    hints = [decisions["order"], *(h for _, h in decisions["joins"]), *(h for _, h in decisions["scans"])]
    return [Move(((hint_slot(hint), hint),)) for hint in map(_checked(grammar), filter(None, hints)) if hint]


def default_moves(program: str, explain_json, grammar: HintGrammar) -> list[Move]:
    if program == CARD:
        return plan_moves(CARD, explain_json, grammar)
    decisions = plan_decisions(explain_json)
    hints = [decisions["order"]]
    for scope, _ in decisions["joins"]:
        hints += [f"{head}({' '.join(scope)})" for head in ("HashJoin", "MergeJoin", "NestLoop")]
    for alias, _ in decisions["scans"]:
        hints += [f"SeqScan({alias})"]
        if grammar.indexes.get(alias):
            hints += [f"IndexScan({alias})", f"BitmapScan({alias})"]
    moves: list[Move] = []
    for hint in map(_checked(grammar), filter(None, hints)):
        if hint:
            add_move(moves, Move(((hint_slot(hint), hint),)))
    return moves


def parse_recipe(program: str, content: str, *, base: Config, grammar: HintGrammar, base_plan) -> Recipe:
    rejected: list[dict] = []
    try:
        payload = _json_object(content)
    except ValueError as exc:
        payload, rejected = {}, [{"entry": content[:200], "reason": str(exc)}]
    parse = _parse_card if program == CARD else _parse_hint
    moves, seeds = parse(payload.get("neighborhood"), payload.get("seeds"), grammar, rejected)
    recipe = Recipe(program, base, moves, seeds, diagnosis=str(payload.get("diagnosis") or ""), rejected=rejected)
    if not moves:
        recipe.moves, recipe.default = default_moves(program, base_plan, grammar), True
    cover_base(recipe)
    return recipe


def default_recipe(program: str, *, base: Config, grammar: HintGrammar, base_plan) -> Recipe:
    recipe = Recipe(program, base, default_moves(program, base_plan, grammar), [], default=True)
    cover_base(recipe)
    return recipe


def cover_base(recipe: Recipe) -> None:
    for slot, value in recipe.base.component(recipe.program).items():
        add_move(recipe.moves, Move(((slot, (value, value) if recipe.program == CARD else value),)))


def _parse_hint(neighborhood, seeds, grammar: HintGrammar, rejected: list) -> tuple[list[Move], list[dict]]:
    moves: list[Move] = []
    pools = neighborhood if isinstance(neighborhood, dict) else {}
    if neighborhood is not None and not isinstance(neighborhood, dict):
        rejected.append({"entry": neighborhood, "reason": "neighborhood must be an object of pools"})
    for pool in ("access_path", "join_method", "join_order"):
        for hint in _list(pools.get(pool)):
            checked = _hint_or_reject(grammar, hint, rejected)
            if checked:
                add_move(moves, Move(((hint_slot(checked), checked),)))
    groups = [("coupled", group) for group in _list(pools.get("coupled"))]
    groups += [("seed", group) for group in _list(seeds)]
    seed_values = []
    for kind, group in groups:
        checked = [_hint_or_reject(grammar, hint, rejected) for hint in _list(group)]
        if not group or not all(checked):
            continue
        items = tuple((hint_slot(hint), hint) for hint in checked)
        if len({slot for slot, _ in items}) != len(items):
            rejected.append({"entry": group, "reason": "assigns one decision twice"})
            continue
        for item in items:
            add_move(moves, Move((item,)))
        if kind == "coupled" and len(items) > 1:
            add_move(moves, Move(tuple(sorted(items))))
        if kind == "seed":
            seed_values.append(dict(items))
    return moves, seed_values


def _parse_card(neighborhood, seeds, grammar: HintGrammar, rejected: list) -> tuple[list[Move], list[dict]]:
    moves: list[Move] = []
    for group in _list(neighborhood):
        try:
            items = tuple(sorted((grammar.scope(scope), _range(bounds)) for scope, bounds in dict(group).items()))
        except (TypeError, ValueError) as exc:
            rejected.append({"entry": group, "reason": str(exc)})
            continue
        if not items or len({scope for scope, _ in items}) != len(items):
            rejected.append({"entry": group, "reason": "empty group or repeated scope"})
            continue
        add_move(moves, Move(items))
    seed_values = []
    for seed in _list(seeds):
        try:
            values = {grammar.scope(scope): _finite(value) for scope, value in dict(seed).items()}
        except (TypeError, ValueError) as exc:
            rejected.append({"entry": seed, "reason": str(exc)})
            continue
        if values:
            for scope, value in values.items():
                if not any(scope in move.slots for move in moves):
                    add_move(moves, Move(((scope, (value, value)),)))
            seed_values.append(values)
    return moves, seed_values


def _json_object(content: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip())
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("response is not a JSON object")  # noqa: TRY004 - parse errors share one type
    return value


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _range(bounds) -> tuple[float, float]:
    low, high = (_finite(v) for v in bounds)
    if low > high:
        raise ValueError(f"empty range {bounds}")
    return float(low), float(high)


def _finite(value) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("log-factor must be finite")
    return value


def _hint_or_reject(grammar: HintGrammar, hint, rejected: list) -> str | None:
    try:
        checked = grammar.check(hint)
        if hint_slot(checked)[0] == "rows":
            raise ValueError("Rows belongs to the cardinality program")
        return checked
    except (TypeError, ValueError) as exc:
        rejected.append({"entry": hint, "reason": str(exc)})
        return None


def _checked(grammar: HintGrammar):
    def check(hint):
        try:
            return grammar.check(hint)
        except ValueError:
            return None
    return check


def _valid_scope(grammar: HintGrammar, scope) -> bool:
    try:
        grammar.scope(" ".join(scope))
        return True
    except ValueError:
        return False
