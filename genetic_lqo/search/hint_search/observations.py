from __future__ import annotations

import copy
import math
import re

from genetic_lqo.search.hint_search.models import LeadingTree, leading_hint

_JOINS = {"Hash Join": "HashJoin", "Merge Join": "MergeJoin", "Nested Loop": "NestLoop"}
_SCANS = {"Seq Scan": "SeqScan", "Index Scan": "IndexScan", "Index Only Scan": "IndexOnlyScan",
          "Bitmap Heap Scan": "BitmapScan"}


def _root(explain_json):
    top = explain_json[0] if isinstance(explain_json, list) and explain_json else explain_json
    return top.get("Plan", top) if isinstance(top, dict) else None


def plan_decisions(explain_json) -> dict:
    joins, scans = [], []

    def walk(node) -> LeadingTree | None:
        children = [walk(child) for child in node.get("Plans", []) if child.get("Parent Relationship")
                    not in ("SubPlan", "InitPlan")]
        children = [child for child in children if child is not None]
        kind = node.get("Node Type", "")
        if kind in _SCANS and node.get("Alias"):
            alias = node["Alias"]
            index = node.get("Index Name")
            body = f"{alias} {index}" if index and kind in ("Index Scan", "Index Only Scan") else alias
            scans.append((alias, f"{_SCANS[kind]}({body})"))
            return alias
        if kind == "Bitmap Index Scan":
            return None
        if kind in _JOINS and len(children) == 2:
            tree = (children[0], children[1])
            scope = tuple(sorted(_leaves(tree)))
            joins.append((scope, f"{_JOINS[kind]}({' '.join(scope)})"))
            return tree
        return children[0] if len(children) == 1 else None

    root = _root(explain_json)
    tree = walk(root) if root else None
    return {"order": leading_hint(tree) if isinstance(tree, tuple) else None, "joins": joins, "scans": scans,
            "scopes": [scope for scope, _ in joins]}


def _leaves(tree):
    return _leaves(tree[0]) + _leaves(tree[1]) if isinstance(tree, tuple) else (tree,)


def plan_text(explain_json, *, analyze: bool = True) -> str:
    lines: list[str] = []

    def walk(node, depth):
        kind = node.get("Node Type", "?")
        if node.get("Join Type") and kind in _JOINS and node["Join Type"] != "Inner":
            kind += f" ({node['Join Type']})"
        target = " ".join(filter(None, [node.get("Alias"), node.get("Index Name") and f"using {node['Index Name']}"]))
        parts = [f"{kind} {target}".strip(), f"est {_rows(node.get('Plan Rows'))} rows"]
        if analyze and "Actual Rows" in node:
            loops = node.get("Actual Loops") or 1
            parts.append(f"actual {_rows(node['Actual Rows'] * loops)} rows")
            if loops > 1:
                parts.append(f"{loops} loops")
            if node.get("Actual Total Time") is not None:
                parts.append(f"{node['Actual Total Time'] * loops:.1f} ms")
        elif analyze:
            parts.append("never executed")
        lines.append("  " * depth + "-> " + ", ".join(parts))
        for key in ("Hash Cond", "Merge Cond", "Join Filter", "Index Cond", "Filter"):
            if node.get(key):
                lines.append("  " * depth + f"     {key}: {node[key]}")
        for child in node.get("Plans", []):
            walk(child, depth + 1)

    root = _root(explain_json)
    if root:
        walk(root, 0)
    return "\n".join(lines)


def _rows(value) -> str:
    if value is None:
        return "?"
    return f"{value:,.0f}"


def compact_statistics(entries):
    result = copy.deepcopy(entries)
    for entry in result:
        for stats in entry["node"]["postgresScan"]["baseConf"]["statistics"]["columnStats"]:
            for name, limit in (("most_common_values", 5), ("most_common_frequencies", 5),
                                ("histogram_bounds", 7)):
                values = stats[name]
                if len(values) > limit:
                    stats[name + "_total_count"] = len(values)
                    if name == "histogram_bounds":
                        indices = [round(i * (len(values)-1)/(limit-1)) for i in range(limit)]
                        stats[name] = [values[i] for i in indices]
                    else:
                        stats[name] = values[:limit]
    return result


def table_summary(sql: str, aliases: dict[str, str], statistics, indexes) -> str:
    used = {(alias, column) for alias, column in re.findall(r"\b(\w+)\.(\w+)\b", sql)}
    lines = []
    for entry in statistics:
        scan = entry["node"]["postgresScan"]
        alias, base = scan["alias"], scan["baseConf"]
        rows = base["statistics"]["n_rows"]
        lines.append(f"- {alias}: {scan['relation']}, about {_rows(rows)} rows")
        for stats in base["statistics"]["columnStats"]:
            if (alias, stats["column"]) not in used:
                continue
            distinct = stats.get("estimated_distinct_values")
            facts = [f"{_rows(distinct)} distinct" if distinct is not None else None,
                     f"{stats['null_fraction']:.0%} null" if stats.get("null_fraction") else None]
            mcv = stats.get("most_common_values") or []
            if mcv:
                facts.append("common values " + ", ".join(map(str, mcv[:3])))
            lines.append(f"    {stats['column']}: " + "; ".join(fact for fact in facts if fact))
        names = [index["index_name"] for index in indexes.get(alias, [])]
        if names:
            lines.append(f"    indexes: {', '.join(names)}")
    return "\n".join(lines)


def plan_nodes(explain_json):
    top = _root(explain_json)
    if not isinstance(top, dict):
        return []
    records = []

    def walk(node, path):
        index = len(records)
        records.append(None)
        children = [walk(child, f"{path}.{i}") for i, child in enumerate(node.get("Plans", []))]
        aliases = sorted(set([node["Alias"]] if node.get("Alias") else []).union(
            *(set(child) for child in children)))
        fields = ("Node Type", "Join Type", "Alias", "Relation Name", "Index Name", "Parent Relationship",
                  "Filter", "Index Cond", "Hash Cond", "Merge Cond", "Join Filter", "Plan Rows", "Plan Width",
                  "Total Cost", "Actual Rows", "Actual Loops", "Rows Removed by Filter", "Rows Removed by Join Filter",
                  "Shared Hit Blocks", "Shared Read Blocks", "Temp Read Blocks", "Temp Written Blocks",
                  "Hash Batches", "Original Hash Batches", "Hash Buckets", "Peak Memory Usage", "Workers Launched")
        records[index] = {"path": path, "aliases": aliases, "child_aliases": children,
                          **{key: node[key] for key in fields if key in node}}
        return aliases
    walk(top, "0")
    return records


def response_observation(result):
    if result.success:
        elapsed = result.elapsed_wall_seconds
        return (elapsed, False) if elapsed is not None and math.isfinite(elapsed) and elapsed > 0 else None
    if result.timed_out and result.timeout_seconds:
        return result.timeout_seconds, True
    return None
