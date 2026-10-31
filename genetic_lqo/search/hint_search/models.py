from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from genetic_lqo.planning.dialect import (
    extract_target_tables,
    format_hint_identifier,
    normalize_hint,
    parse_hint,
    parse_rows_hint,
)

CARD, HINT = "card", "hint"
PROGRAMS = (CARD, HINT)
JOIN_HEADS = ("HashJoin", "MergeJoin", "NestLoop", "NoHashJoin", "NoMergeJoin", "NoNestLoop")
SCAN_HEADS = ("SeqScan", "IndexScan", "IndexOnlyScan", "BitmapScan",
              "NoSeqScan", "NoIndexScan", "NoIndexOnlyScan", "NoBitmapScan")
INDEX_SCAN_HEADS = ("IndexScan", "IndexOnlyScan", "BitmapScan")
PC_HEADS = frozenset(("Leading", *JOIN_HEADS, *SCAN_HEADS))
LOG_STEP = 0.25
DEFAULT_RANGE = (-2.0, 2.0)

Scope = tuple[str, ...]
Slot = tuple


def opposite(program: str) -> str:
    return HINT if program == CARD else CARD


def quantize(value: float) -> float:
    return round(round(value / LOG_STEP) * LOG_STEP, 2) + 0.0


def rows_hint(scope: Scope, log_factor: float) -> str:
    factor = f"{10 ** log_factor:.6g}"
    return f"Rows({' '.join(map(format_hint_identifier, scope))} *{factor})"


def hint_slot(hint: str) -> Slot:
    head, _ = parse_hint(hint)
    scope = tuple(sorted(extract_target_tables(hint)))
    if head == "Leading":
        return ("order",)
    if head in JOIN_HEADS:
        return ("join", *scope)
    if head in SCAN_HEADS:
        return ("scan", *scope)
    if head == "Rows":
        return ("rows", *scope)
    raise ValueError(f"Unsupported hint: {hint}")


@dataclass(frozen=True)
class Config:

    card: tuple[tuple[Scope, float], ...] = ()
    hint: tuple[tuple[Slot, str], ...] = ()

    @classmethod
    def make(cls, card: Mapping[Scope, float] | None = None, hint: Mapping[Slot, str] | None = None) -> Config:
        return cls(tuple(sorted((tuple(s), quantize(v)) for s, v in (card or {}).items())),
                   tuple(sorted((hint or {}).items())))

    def component(self, program: str) -> dict:
        return dict(self.card if program == CARD else self.hint)

    def with_component(self, program: str, values: Mapping) -> Config:
        if program == CARD:
            return Config.make(values, dict(self.hint))
        return Config.make(dict(self.card), values)

    @property
    def size(self) -> int:
        return len(self.card) + len(self.hint)

    def hints(self) -> tuple[str, ...]:
        return tuple(value for _, value in self.hint) + tuple(rows_hint(s, v) for s, v in self.card)

    def describe(self) -> dict:
        return {"hints": [value for _, value in self.hint],
                "cardinality": {" ".join(scope): value for scope, value in self.card}}


@dataclass(frozen=True)
class HintCandidate:

    hints: tuple[str, ...] = ()

    def comment(self) -> str:
        return "/*+ " + " ".join(self.hints) + " */" if self.hints else ""


LeadingTree = str | tuple


def parse_leading_tree(hint: str) -> LeadingTree | None:
    head, body = parse_hint(hint)
    if head != "Leading":
        return None
    tokens = re.findall(r'"(?:[^"]|"")*"|[()]|[^\s()]+', body)

    def parse(position):
        if tokens[position] != "(":
            if tokens[position] == ")":
                raise ValueError("Unexpected closing parenthesis")
            return tokens[position], position + 1
        left, position = parse(position + 1)
        right, position = parse(position)
        if tokens[position] != ")":
            raise ValueError("Expected binary Leading tree")
        return (left, right), position + 1

    try:
        tree, end = parse(0)
    except (IndexError, ValueError):
        return None
    if end != len(tokens) or not isinstance(tree, tuple):
        return None
    leaves = _leaves(tree)
    return tree if len(leaves) == len(set(leaves)) else None


def leading_hint(tree: LeadingTree) -> str:
    def render(node):
        return format_hint_identifier(node) if isinstance(node, str) else f"({render(node[0])} {render(node[1])})"
    return f"Leading({render(tree)})"


def _leaves(tree: LeadingTree) -> tuple[str, ...]:
    return _leaves(tree[0]) + _leaves(tree[1]) if isinstance(tree, tuple) else (tree,)


class HintGrammar:

    def __init__(self, aliases: Iterable[str], indexes: Mapping[str, list[dict]]):
        self.aliases = tuple(aliases)
        if not self.aliases or len(set(self.aliases)) != len(self.aliases):
            raise ValueError("Unique nonempty query aliases are required")
        self.indexes = dict(indexes)

    def check(self, hint: str) -> str:
        if not isinstance(hint, str) or not hint.strip() or "/*" in hint or "*/" in hint:
            raise ValueError(f"Not a hint expression: {hint!r}")
        hint = normalize_hint(hint)
        head, body = parse_hint(hint)
        if head == "Leading" and "(" in body and parse_leading_tree(hint) is None:
            missing = body.count(")") - body.count("(")
            balanced = "(" * max(0, missing) + body + ")" * max(0, -missing)
            for candidate in (f"Leading({balanced})", f"Leading(({balanced}))"):
                if parse_leading_tree(candidate) is not None:
                    hint = candidate
                    head, body = parse_hint(hint)
                    break
        targets = extract_target_tables(hint)
        if head != "Rows" and head not in PC_HEADS:
            raise ValueError(f"Unsupported hint: {hint}")
        unknown = set(targets) - set(self.aliases)
        if unknown or not targets or len(set(targets)) != len(targets):
            raise ValueError(f"Unknown or repeated aliases in {hint}")
        if head == "Rows":
            scope, operator, value = parse_rows_hint(hint)
            if not scope or value is None or not math.isfinite(value) or (operator == "#" and value < 1):
                raise ValueError(f"Invalid Rows hint: {hint}")
        elif head == "Leading":
            if len(targets) < 2 or ("(" in body and parse_leading_tree(hint) is None):
                raise ValueError(f"Invalid Leading hint: {hint}")
        elif head in JOIN_HEADS:
            if len(targets) < 2:
                raise ValueError(f"A join hint needs at least two aliases: {hint}")
        else:
            names = [token.strip('"') for token in re.findall(r'"(?:[^"]|"")*"|[^\s]+', body)[1:]]
            if names and head.removeprefix("No") not in INDEX_SCAN_HEADS:
                raise ValueError(f"Only index scans take index names: {hint}")
            known = {entry.get("index_name") for entry in self.indexes.get(targets[0], [])}
            if set(names) - known:
                raise ValueError(f"Unknown index in {hint}")
        return hint

    def from_hints(self, hints: Iterable[str]) -> HintCandidate:
        if isinstance(hints, (str, bytes)):
            raise ValueError("Expected a list of hint expressions")  # noqa: TRY004 - shared grammar contract
        checked = [self.check(hint) for hint in hints]
        slots = [hint_slot(hint) for hint in checked]
        if len(set(slots)) != len(slots):
            raise ValueError("Conflicting hints for one optimizer decision")
        return HintCandidate(tuple(sorted(checked)))

    def validate(self, candidate: HintCandidate) -> HintCandidate:
        return self.from_hints(candidate.hints)

    def scope(self, text: str) -> Scope:
        scope = tuple(sorted(str(text).replace(",", " ").split()))
        if len(scope) < 2 or len(set(scope)) != len(scope) or set(scope) - set(self.aliases):
            raise ValueError(f"Invalid join scope: {text!r}")
        return scope
