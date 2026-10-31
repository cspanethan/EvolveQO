import json
import random

import pytest
from fake_postgres import SQL, FakeExecutor

from genetic_lqo.search.hint_search.engine import Task
from genetic_lqo.search.hint_search.models import CARD, HINT, Config, HintGrammar, hint_slot, quantize
from genetic_lqo.search.hint_search.observations import plan_decisions
from genetic_lqo.search.hint_search.recipe import Move, Recipe, parse_recipe, plan_moves

GRAMMAR = HintGrammar(("a", "b", "c"), {"b": [{"index_name": "tb_pkey"}]})


def native_plan():
    return FakeExecutor().explain(SQL).explain_json


def test_alternatives_for_one_decision_share_a_slot_and_join_order_is_one_slot():
    assert hint_slot("IndexScan(b)") == hint_slot("SeqScan(b)") != hint_slot("SeqScan(a)")
    assert hint_slot("HashJoin(b a)") == hint_slot("NestLoop(a b)")
    assert hint_slot("Leading(((a b) c))") == hint_slot("Leading(((b a) c))") == ("order",)
    with pytest.raises(ValueError, match="Conflicting"):
        GRAMMAR.from_hints(["IndexScan(b)", "SeqScan(b)"])
    assert GRAMMAR.from_hints(["IndexScan(b)", "SeqScan(a)"]).hints == ("IndexScan(b)", "SeqScan(a)")


@pytest.mark.parametrize("hint", ["HashJoin(a x)", "IndexScan(b missing_idx)", "Leading(((a b) a))",
                                  "SeqScan(a b)", "Set(enable_hashjoin off)", "HashJoin(a)"])
def test_grammar_rejects_invalid_hints(hint):
    with pytest.raises(ValueError):
        GRAMMAR.check(hint)


def test_config_renders_log_factors_as_rows_and_compares_by_value():
    config = Config.make(card={("a", "b"): -2.1}, hint={("join", "a", "b"): "NestLoop(a b)"})
    assert config.hints() == ("NestLoop(a b)", "Rows(a b *0.01)")
    assert config == Config.make(card={("a", "b"): -2.0}, hint={("join", "a", "b"): "NestLoop(a b)"})
    assert config.size == 2 and quantize(0.13) == 0.25


def test_plan_decisions_cover_order_methods_and_access_paths():
    decisions = plan_decisions(native_plan())
    assert decisions["order"] == "Leading(((a b) c))"
    assert ("a", "b") in decisions["scopes"] and ("a", "b", "c") in decisions["scopes"]
    assert sorted(h for _, h in decisions["scans"]) == ["SeqScan(a)", "SeqScan(b)", "SeqScan(c)"]
    assert len(plan_moves(CARD, native_plan(), GRAMMAR)) == 2
    assert len(plan_moves(HINT, native_plan(), GRAMMAR)) == 6


def test_parse_hint_recipe_drops_bad_entries_and_keeps_coupled_hints_editable():
    content = json.dumps({
        "diagnosis": "nested loop over b",
        "neighborhood": {"access_path": ["IndexScan(b tb_pkey)", "IndexScan(z)"], "join_method": ["NestLoop(a b)"],
                         "join_order": ["Leading(((b a) c))"],
                         "coupled": [["Leading(((b a) c))", "NestLoop(a b)"], ["SeqScan(b)", "IndexScan(b)"]]},
        "seeds": [["Leading(((b a) c))", "NestLoop(a b)"], ["Rows(a b #10)"]]})
    base = Config.make(hint={("scan", "c"): "SeqScan(c)"})
    recipe = parse_recipe(HINT, content, base=base, grammar=GRAMMAR, base_plan=native_plan())
    singles = {move.items[0][1] for move in recipe.moves if not move.coupled}
    coupled = [move for move in recipe.moves if move.coupled]
    assert {"IndexScan(b tb_pkey)", "NestLoop(a b)", "Leading(((b a) c))", "SeqScan(c)"} <= singles
    assert len(coupled) == 1 and set(coupled[0].slots) == {("order",), ("join", "a", "b")}
    assert recipe.seeds == [{("order",): "Leading(((b a) c))", ("join", "a", "b"): "NestLoop(a b)"}]
    reasons = " ".join(r["reason"] for r in recipe.rejected)
    assert "twice" in reasons and "cardinality program" in reasons and not recipe.default
    assert recipe.diagnosis == "nested loop over b"


def test_parse_card_recipe_and_default_fallback():
    content = json.dumps({"neighborhood": [{"a b": [-3, -1]}, {"a b c": [-2, 0], "b c": [-2, 0]},
                                           {"a z": [0, 1]}, {"a b": [1, -1]}],
                          "seeds": [{"a b": -2}]})
    base = Config.make(card={("b", "c"): 1.0})
    recipe = parse_recipe(CARD, content, base=base, grammar=GRAMMAR, base_plan=native_plan())
    assert Move(((("a", "b"), (-3.0, -1.0)),)) in recipe.moves
    assert Move(((("b", "c"), (1.0, 1.0)),)) in recipe.moves
    assert any(move.coupled for move in recipe.moves) and len(recipe.rejected) == 2
    fallback = parse_recipe(CARD, "not json", base=Config(), grammar=GRAMMAR, base_plan=native_plan())
    assert fallback.default and all(move.items[0][1] == (-2.0, 2.0) for move in fallback.moves)


def task(program=HINT, moves=None, seeds=()):
    recipe = Recipe(program, Config(), moves or [Move(((("join", "a", "b"), "NestLoop(a b)"),)),
                                                  Move(((("scan", "b"), "IndexScan(b)"),))], list(seeds))
    return Task(0, recipe, 1.0, "native", random.Random(1))


def test_offspring_differ_by_crossover_plus_exactly_one_edit_and_are_never_repeated():
    t = task(seeds=[{("join", "a", "b"): "NestLoop(a b)"}])
    seed = t.next_candidate()
    assert seed == Config.make(hint={("join", "a", "b"): "NestLoop(a b)"})
    seen = {Config(), seed}
    t.observe(seed, "nl", 0.5, False)
    while (child := t.next_candidate()) is not None:
        assert child not in seen
        before, after = seed.component(HINT), child.component(HINT)
        assert len(set(before.items()) ^ set(after.items())) <= 4
        seen.add(child)
        t.observe(child, f"plan{len(seen)}", 0.5, False)
    assert not t.can_breed() and len(seen) == 4


def test_population_keeps_fewest_assignments_per_plan_and_censored_ranks_last():
    t = task()
    big = Config.make(hint={("join", "a", "b"): "NestLoop(a b)", ("scan", "b"): "IndexScan(b)"})
    small = Config.make(hint={("join", "a", "b"): "NestLoop(a b)"})
    t.observe(big, "p1", 0.3, False)
    t.observe(small, "p1", 0.3, False)
    t.observe(big, "p1", 0.3, False)
    assert t.population["p1"].config == small
    t.observe(Config.make(hint={("scan", "b"): "IndexScan(b)"}), "p2", 2.0, True)
    members = list(t.population.values())
    winners = {t._tournament(members).config for _ in range(50)}
    assert t.population["p2"].config not in winners or len(winners) > 1
    assert t.best_seconds == 0.3


def test_card_moves_draw_on_the_grid_and_enumerate_their_outcomes():
    move = Move(((("a", "b"), (-1.0, 0.0)),))
    assert [o[("a", "b")] for o in move.outcomes()] == [-1.0, -0.75, -0.5, -0.25, 0.0]
    drawn = move.apply({}, random.Random(3))[("a", "b")]
    assert drawn in (-1.0, -0.75, -0.5, -0.25, 0.0)


def test_leading_without_the_outermost_pair_is_repaired():
    grammar = HintGrammar(("a", "b", "c", "d"), {})
    assert grammar.check("Leading(((a b) c) d)") == "Leading((((a b) c) d))"
    assert grammar.check("Leading((((a b) c) d))") == "Leading((((a b) c) d))"
    assert grammar.check("Leading(((a b) c) d))") == "Leading((((a b) c) d))"
    with pytest.raises(ValueError):
        grammar.check("Leading(((a b) c) a)")
