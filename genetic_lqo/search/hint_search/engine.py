from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from itertools import islice

from genetic_lqo.search.hint_search.models import Config
from genetic_lqo.search.hint_search.recipe import Move, Recipe, add_move

_BREED_ATTEMPTS = 32


@dataclass
class Member:
    config: Config
    seconds: float
    censored: bool = False


@dataclass
class Task:
    id: int
    recipe: Recipe
    base_seconds: float
    base_fingerprint: str
    rng: random.Random
    population: dict[str, Member] = field(default_factory=dict)
    waiting: deque = field(default_factory=deque)
    generated: set = field(default_factory=set)
    observations: list[tuple[float, bool]] = field(default_factory=list)
    best_seconds: float = float("inf")

    def __post_init__(self) -> None:
        self.moves: list[Move] = list(self.recipe.moves)
        base = self.recipe.base
        self.population[self.base_fingerprint] = Member(base, self.base_seconds)
        self.generated.add(base)
        for seed in self.recipe.seeds:
            config = base.with_component(self.program, seed)
            if config not in self.generated:
                self.generated.add(config)
                self.waiting.append(config)

    @property
    def program(self) -> str:
        return self.recipe.program

    def next_candidate(self) -> Config | None:
        if self.waiting:
            return self.waiting.popleft()
        return self.breed()

    def breed(self) -> Config | None:
        members = list(self.population.values())
        for _ in range(_BREED_ATTEMPTS):
            child = self._mutate(self._crossover(self._tournament(members), self._tournament(members)))
            if child not in self.generated:
                return self._accept(child)
        unseen = list(islice((c for c in self._single_edits() if c not in self.generated), _BREED_ATTEMPTS))
        return self._accept(self.rng.choice(unseen)) if unseen else None

    def can_breed(self) -> bool:
        return bool(self.waiting) or any(config not in self.generated for config in self._single_edits())

    def observe(self, config: Config, fingerprint: str, seconds: float, censored: bool) -> None:
        self.observations.append((seconds, censored))
        if not censored:
            self.best_seconds = min(self.best_seconds, seconds)
        current = self.population.get(fingerprint)
        if current is None or config.size < current.config.size:
            self.population[fingerprint] = Member(config, seconds, censored)

    def grow(self, moves: list[Move]) -> None:
        for move in moves:
            add_move(self.moves, move)

    def _accept(self, config: Config) -> Config:
        self.generated.add(config)
        return config

    def _tournament(self, members: list[Member]) -> Member:
        a, b = self.rng.choice(members), self.rng.choice(members)
        key = lambda m: (m.censored, m.seconds)
        return a if key(a) <= key(b) else b

    def _crossover(self, a: Member, b: Member) -> dict:
        if a is b:
            return a.config.component(self.program)
        left, right = a.config.component(self.program), b.config.component(self.program)
        child = {}
        for slot in left.keys() | right.keys():
            if slot in left and slot in right:
                child[slot] = left[slot] if self.rng.random() < 0.5 else right[slot]
            elif self.rng.random() < 0.5:
                child[slot] = left.get(slot, right.get(slot))
        return child

    def _mutate(self, values: dict) -> Config:
        edits = [("move", move) for move in self.moves] + [("release", slot) for slot in values]
        kind, target = self.rng.choice(edits)
        if kind == "release":
            values = {slot: value for slot, value in values.items() if slot != target}
        else:
            values = target.apply(values, self.rng)
        return self.recipe.base.with_component(self.program, values)

    def _single_edits(self):
        base = self.recipe.base
        for member in list(self.population.values()):
            values = member.config.component(self.program)
            for slot in values:
                yield base.with_component(self.program, {s: v for s, v in values.items() if s != slot})
            for move in self.moves:
                for outcome in move.outcomes():
                    yield base.with_component(self.program, {**values, **outcome})
