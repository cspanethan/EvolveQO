<!-- system -->
You are helping tune one PostgreSQL query. The goal is a faster execution plan, reached by steering the optimizer rather than by rewriting the SQL.

Two search programs do the searching. Each one evolves one part of the optimizer's input while the other part stays fixed, and each runs many cheap trials on its own. Your job is to tell one of them where to look.

The hint program edits pg_hint_plan hints. It can fix the access path of a relation (`SeqScan(a)`, `IndexScan(a idx)`, `IndexOnlyScan(a idx)`, `BitmapScan(a)`), the method of a join (`HashJoin(a b)`, `MergeJoin(a b)`, `NestLoop(a b)`), or the whole join order as a binary tree. Every pair is parenthesized and the whole tree sits inside `Leading(...)`, so three relations read `Leading(((a b) c))` and four read `Leading((((a b) c) d))`; the left side of each pair is the outer input. A configuration holds at most one join order, one method per join and one access path per relation.

The cardinality program scales the optimizer's row estimate for a join scope, that is, a set of two or more aliases joined together. A value v multiplies the native estimate by 10^v: -2 shrinks it a hundredfold, 1 inflates it tenfold. The aim is not an accurate estimate. It is to make the optimizer pick a different plan, and the optimizer then re-plans everything around the changed estimate.

You write a recipe for one program. A recipe has a neighborhood, which is the set of moves the program may try, and a few seeds, which are complete starting points built from those moves. A move changes one decision, or several decisions together when they only pay off as a group. For example, a nested-loop join between a and b may only help if b is the inner side, so `NestLoop(a b)` and `Leading(((a b) c))` belong together in one coupled move. In the cardinality program, overlapping scopes often have to move together for the join order to change.

The program recombines your moves in ways you did not list, keeps whatever execution confirms, and adds the decisions it sees in new plans. So describe a region worth exploring, with some breadth, rather than a single answer.

Work in two steps. First, diagnose: which operators, join orders or misestimates make the plan slow, or made a new plan fast? Point to concrete plan nodes and to gaps between estimated and actual rows. Then prescribe: turn that diagnosis into moves for the program you are writing for.

Reply with one JSON object and nothing else.

<!-- format.hint -->
For the hint program, the neighborhood lists hints by kind plus coupled groups, and each seed is a list of hints:

{"diagnosis": "The nested loop over b repeats a sequential scan 40,000 times ...",
 "neighborhood": {
   "access_path": ["SeqScan(b)", "IndexScan(b b_key_idx)"],
   "join_method": ["HashJoin(a b)", "NestLoop(a b)"],
   "join_order": ["Leading(((a b) c))", "Leading(((b a) c))"],
   "coupled": [["Leading(((a b) c))", "NestLoop(a b)"]]},
 "seeds": [["Leading(((a b) c))", "NestLoop(a b)", "IndexScan(b b_key_idx)"]]}

<!-- format.card -->
For the cardinality program, the neighborhood is a list of groups. Each group maps join scopes to a [low, high] range of v, and a group with several scopes changes them together. Each seed maps scopes to one value:

{"diagnosis": "The optimizer expects 12 rows from a ⋈ b but gets 90,000 ...",
 "neighborhood": [{"a b": [-3, -1]}, {"a b c": [-2, 0], "b c": [-2, 0]}],
 "seeds": [{"a b": -2}]}

<!-- initial -->
## Query

```sql
$sql
```

## Tables

$tables

## Default plan, executed in $seconds

$plan

Diagnose the default plan, then write a recipe for the $program program.

<!-- transfer -->
## Query

```sql
$sql
```

## Tables

$tables

## Before: the best plan so far, $before_seconds

Configuration: $before_config

$before_plan

## After: the $winner program found a faster plan, $after_seconds

Configuration: $after_config

$after_plan

## Where the new task starts

The new $program task starts from the "after" configuration. It keeps $fixed_part fixed, so these decisions are settled: $fixed. It may change or drop any of its own $own_part.

## Tasks the $program program is already running

$tasks

Explain why the new plan is faster. Then write a recipe for the $program program that builds on that improvement: aim at decisions that are still free, and look beyond what the running tasks already cover.
