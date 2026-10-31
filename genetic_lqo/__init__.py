from typing import TYPE_CHECKING

from genetic_lqo.core.types import GeneticSearchConfig

if TYPE_CHECKING:
    from genetic_lqo.search.optimizer import GeneticLQO, HintSearchResult, run_genetic_lqo

__all__ = ["GeneticLQO", "GeneticSearchConfig", "HintSearchResult", "run_genetic_lqo"]


def __getattr__(name: str):
    if name in {"GeneticLQO", "HintSearchResult", "run_genetic_lqo"}:
        from .search import optimizer

        value = getattr(optimizer, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
