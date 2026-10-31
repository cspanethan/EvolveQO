from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable

from genetic_lqo.core.types import (
    HintGene,
)
from genetic_lqo.planning.hint_scope import (
    NEGATIVE_JOIN_METHODS,
    NEGATIVE_SCAN_METHODS,
    POSITIVE_JOIN_METHODS,
    POSITIVE_SCAN_METHODS,
    REGEXP_METHODS,
)

_HINT_HEAD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$")
_CORRECTION_RE = re.compile(r"^[#*+\-]\S+$")
_REGEXP_ACCESS_HINTS = frozenset(REGEXP_METHODS)
_ACCESS_HINTS = frozenset((*POSITIVE_SCAN_METHODS, *REGEXP_METHODS))
_ACCESS_AVOID_HINTS = frozenset(NEGATIVE_SCAN_METHODS)
_JOIN_METHOD_HINTS = frozenset(POSITIVE_JOIN_METHODS)
_JOIN_METHOD_AVOID_HINTS = frozenset(NEGATIVE_JOIN_METHODS)
_MEMOIZE_HINTS = {"Memoize", "NoMemoize"}
CARDINALITY_INTERVENTION_INTENT = "cardinality_intervention"
_KNOWN_HINT_HEADS = frozenset(
    {
        *_ACCESS_HINTS,
        *_ACCESS_AVOID_HINTS,
        *_JOIN_METHOD_HINTS,
        *_JOIN_METHOD_AVOID_HINTS,
        *_MEMOIZE_HINTS,
        "DisableIndex",
        "Leading",
        "Parallel",
        "Rows",
        "Set",
    }
)
_CANONICAL_HINT_HEADS = {head.casefold(): head for head in _KNOWN_HINT_HEADS}
_PLANNER_ONLY_SET_GUCS = frozenset(
    {
        "enable_hashjoin",
        "enable_mergejoin",
        "enable_nestloop",
        "from_collapse_limit",
        "geqo",
        "geqo_seed",
        "join_collapse_limit",
    }
)


def normalize_hint(hint: str) -> str:
    text = str(hint or "").strip()
    out: list[str] = []
    pending_space = False
    in_quotes = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_quotes:
            out.append(char)
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    out.append('"')
                    index += 1
                else:
                    in_quotes = False
            index += 1
            continue
        if char == '"':
            if pending_space and out and out[-1] not in "(,":
                out.append(" ")
            pending_space = False
            in_quotes = True
            out.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and out and out[-1] not in "(," and char not in "),":
                out.append(" ")
            pending_space = False
            out.append(char)
        index += 1
    return "".join(out)


def deduplicate_hints(hints: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        normalized = normalize_hint(hint)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def parse_hint(hint: str) -> tuple[str, str]:
    match = _HINT_HEAD_RE.match(normalize_hint(hint))
    if not match:
        return "", ""
    return match.group(1), match.group(2).strip()


def infer_hint_type(hint: str) -> str:
    head, _ = parse_hint(hint)
    if head in _ACCESS_HINTS:
        return "access_path"
    if head in _ACCESS_AVOID_HINTS or head == "DisableIndex":
        return "access_avoid"
    if head in _JOIN_METHOD_HINTS:
        return "join_method"
    if head in _JOIN_METHOD_AVOID_HINTS:
        return "join_method_avoid"
    if head == "Leading":
        return "join_order"
    if head == "Rows":
        return "row_estimate"
    if head == "Parallel":
        return "parallel"
    if head in _MEMOIZE_HINTS:
        return "memoize"
    if head == "Set":
        return "guc"
    return "unknown"


def hint_diagnostic_identity(hint: str) -> tuple[str, ...]:

    normalized = normalize_hint(hint)
    head, body = _diagnostic_hint_parts(normalized)
    if not head:
        return (normalized,)

    tokens = _diagnostic_body_tokens(body)
    values = [_hint_identifier_key(token) for token in tokens]
    if head in _JOIN_METHOD_HINTS | _JOIN_METHOD_AVOID_HINTS | _MEMOIZE_HINTS:
        targets = values
        if len(targets) >= 2:
            return (head, *sorted(targets))
    elif head == "Rows":
        correction = values.pop() if values and _CORRECTION_RE.fullmatch(values[-1]) else ""
        targets = values
        if len(targets) >= 2 and correction:
            return (head, *sorted(targets), "correction", correction)
    elif head in _ACCESS_HINTS | _ACCESS_AVOID_HINTS | {"DisableIndex"}:
        if values:
            return (head, *values)
    elif head == "Leading":
        leading_tokens = _diagnostic_leading_tokens(body)
        if leading_tokens:
            return (
                head,
                *(
                    f"\0{token}" if token in {"(", ")"} else _hint_identifier_key(token)
                    for token in leading_tokens
                ),
            )
    elif head == "Parallel":
        if len(values) == 2:
            values.append("soft")
        if len(values) == 3 and values[-1].casefold() in {"hard", "soft"}:
            return (head, values[0], values[1], values[2].casefold())
    elif head == "Set" and len(values) == 2:
        return (head, *values)
    return (normalized,)


def hint_diagnostic_members_match(actual: Iterable[str], expected: Iterable[str]) -> bool:

    return Counter(hint_diagnostic_identity(hint) for hint in actual) == Counter(
        hint_diagnostic_identity(hint) for hint in expected
    )


def make_gene(hint: str, source: str = "candidate_provider", intent: str = "") -> HintGene:
    normalized = normalize_hint(hint)
    return HintGene(
        hint=normalized,
        hint_type=infer_hint_type(normalized),
        target_tables=tuple(extract_target_tables(normalized)),
        source=source,
        intent=intent or (CARDINALITY_INTERVENTION_INTENT if parse_hint(normalized)[0] == "Rows" else ""),
    )


def build_sql_with_hints(sql: str, hints: Iterable[str]) -> str:
    clean_sql = str(sql or "").strip().rstrip(";")
    hint_list = deduplicate_hints(hints)
    if not hint_list:
        return clean_sql
    return f"/*+ {' '.join(hint_list)} */\n{clean_sql}"


def extract_target_tables(hint: str) -> list[str]:
    head, body = parse_hint(hint)
    if not head:
        return []
    if head == "Set":
        return []
    tokens = _body_alias_tokens(body)
    if head in {"IndexScan", "IndexOnlyScan", "BitmapScan", "DisableIndex"}:
        return [_hint_identifier_key(token) for token in tokens[:1]]
    if head in _ACCESS_HINTS or head in _ACCESS_AVOID_HINTS:
        return [_hint_identifier_key(token) for token in tokens[:1]]
    if head == "Rows":
        if tokens and _CORRECTION_RE.fullmatch(tokens[-1]):
            tokens.pop()
        return [_hint_identifier_key(token) for token in tokens]
    if head == "Parallel":
        if len(tokens) >= 1:
            return [_hint_identifier_key(tokens[0])]
        return []
    return [_hint_identifier_key(token) for token in tokens]


def format_hint_identifier(identifier: str) -> str:
    value = str(identifier)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        return value
    return '"' + value.replace('"', '""') + '"'


def format_left_deep_leading_hint(identifiers: Iterable[str]) -> str:
    formatted = [format_hint_identifier(identifier) for identifier in identifiers]
    if len(formatted) < 2:
        raise ValueError("A left-deep Leading hint requires at least two identifiers")
    expression = formatted[0]
    for identifier in formatted[1:]:
        expression = f"({expression} {identifier})"
    return f"Leading({expression})"


def parse_rows_hint(hint: str) -> tuple[tuple[str, ...], str, float | None]:

    head, body = parse_hint(hint)
    if head != "Rows":
        return (), "", None
    tokens = _body_alias_tokens(body)
    if len(tokens) < 3:
        return (), "", None
    correction = tokens[-1]
    if _CORRECTION_RE.fullmatch(correction) is None:
        return (), "", None
    operator = correction[0]
    try:
        value = float(correction[1:])
    except ValueError:
        return (), "", None
    aliases = tuple(sorted(_hint_identifier_key(token) for token in tokens[:-1]))
    if len(aliases) < 2 or len(set(aliases)) != len(aliases):
        return (), "", None
    return aliases, operator, value


def format_exact_rows_hint(aliases: Iterable[str], rows: int) -> str:

    if type(rows) is not int or rows < 0:
        raise ValueError("Exact Rows value must be a nonnegative integer")
    relation_aliases = tuple(sorted(str(alias) for alias in aliases))
    if len(relation_aliases) < 2 or len(set(relation_aliases)) != len(relation_aliases):
        raise ValueError("Exact Rows hint requires at least two distinct aliases")
    targets = " ".join(format_hint_identifier(alias) for alias in relation_aliases)
    return f"Rows({targets} #{rows})"


def runtime_signature(hints: Iterable[str]) -> tuple[str, ...]:
    signature = []
    for hint in hints:
        normalized = normalize_hint(hint)
        if infer_hint_type(normalized) != "guc":
            continue
        _, body = parse_hint(normalized)
        guc_name, _ = _parse_set_hint(body)
        if guc_name in _PLANNER_ONLY_SET_GUCS:
            continue
        signature.append(normalized.lower())
    return tuple(sorted(signature))


def genes_can_interact(genes: Iterable[HintGene]) -> bool:
    access_by_alias: dict[str, set[str]] = defaultdict(set)
    access_avoid_by_alias: dict[str, set[str]] = defaultdict(set)
    scan_hint_count_by_alias: dict[str, int] = defaultdict(int)
    disabled_index_by_alias: dict[str, set[str]] = defaultdict(set)
    disable_index_hint_count_by_alias: dict[str, int] = defaultdict(int)
    used_index_by_alias: dict[str, set[str]] = defaultdict(set)
    join_method_by_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    join_avoid_by_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    join_directives_by_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    row_by_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    parallel_by_alias: dict[str, set[str]] = defaultdict(set)
    memoize_by_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    memoize_hint_count_by_aliases: dict[tuple[str, ...], int] = defaultdict(int)
    leading_by_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    leading_joinrels: list[tuple[frozenset[str], frozenset[frozenset[str]], bool]] = []
    leading_hint_count = 0
    guc_values: dict[str, set[str]] = defaultdict(set)

    for gene in genes:
        hint = normalize_hint(gene.hint)
        head, body = parse_hint(hint)
        targets = tuple(sorted(extract_target_tables(hint)))
        tokens = _body_alias_tokens(body)

        if head in _ACCESS_HINTS:
            alias = targets[0] if targets else ""
            if not alias:
                return False
            scan_hint_count_by_alias[alias] += 1
            access_by_alias[alias].add(head.removesuffix("Regexp"))
            if head in {"IndexScan", "IndexOnlyScan", "BitmapScan"} and len(tokens) >= 2:
                used_index_by_alias[alias].update(_hint_identifier_key(token) for token in tokens[1:])
        elif head in _ACCESS_AVOID_HINTS:
            alias = targets[0] if targets else ""
            if not alias:
                return False
            scan_hint_count_by_alias[alias] += 1
            access_avoid_by_alias[alias].add(head)
        elif head == "DisableIndex":
            alias = targets[0] if targets else ""
            if not alias or len(tokens) < 2:
                return False
            disable_index_hint_count_by_alias[alias] += 1
            disabled_index_by_alias[alias].update(_hint_identifier_key(token) for token in tokens[1:])
        elif head in _JOIN_METHOD_HINTS:
            if len(targets) < 2:
                return False
            join_method_by_aliases[targets].add(head)
            join_directives_by_aliases[targets].add(hint)
        elif head in _JOIN_METHOD_AVOID_HINTS:
            if len(targets) < 2:
                return False
            join_avoid_by_aliases[targets].add(head.replace("No", "", 1))
            join_directives_by_aliases[targets].add(hint)
        elif head == "Rows":
            if len(targets) < 2:
                return False
            row_by_aliases[targets].add(hint)
        elif head in _MEMOIZE_HINTS:
            if len(targets) < 2:
                return False
            memoize_hint_count_by_aliases[targets] += 1
            memoize_by_aliases[targets].add(head)
        elif head == "Leading":
            if len(targets) < 2:
                return False
            leading_hint_count += 1
            leading_by_aliases[targets].add(_leading_order_key(body))
            joinrels = _leading_joinrel_sets(body)
            if joinrels is None:
                return False
            leading_joinrels.append(joinrels)
        elif head == "Parallel":
            alias = targets[0] if targets else ""
            if not alias:
                return False
            parallel_by_alias[alias].add(hint)
        elif head == "Set":
            guc_name, value = _parse_set_hint(body)
            if not guc_name:
                return False
            guc_values[guc_name].add(value)
        elif not head:
            return False

    if any(count > 1 for count in scan_hint_count_by_alias.values()):
        return False
    if any(count > 1 for count in disable_index_hint_count_by_alias.values()):
        return False

    for alias, methods in access_by_alias.items():
        if len(methods) > 1:
            return False
        if "SeqScan" in methods and "NoSeqScan" in access_avoid_by_alias[alias]:
            return False
        if methods & {"IndexScan", "IndexOnlyScan"} and "NoIndexScan" in access_avoid_by_alias[alias]:
            return False
        if "IndexOnlyScan" in methods and "NoIndexOnlyScan" in access_avoid_by_alias[alias]:
            return False
        if "BitmapScan" in methods and "NoBitmapScan" in access_avoid_by_alias[alias]:
            return False
        if used_index_by_alias[alias] & disabled_index_by_alias[alias]:
            return False

    for aliases, methods in join_method_by_aliases.items():
        if len(methods) > 1:
            return False
        if methods & join_avoid_by_aliases[aliases]:
            return False

    if any(len(values) > 1 for values in join_directives_by_aliases.values()):
        return False

    if any(len(values) > 1 for values in row_by_aliases.values()):
        return False
    if any(len(values) > 1 for values in parallel_by_alias.values()):
        return False
    if any({"Memoize", "NoMemoize"} <= values for values in memoize_by_aliases.values()):
        return False
    if any(count > 1 for count in memoize_hint_count_by_aliases.values()):
        return False
    if leading_hint_count > 1:
        return False
    if any(len(values) > 1 for values in guc_values.values()):
        return False
    if "1" in guc_values.get("join_collapse_limit", set()) and (
        join_directives_by_aliases or row_by_aliases
    ):
        return False

    if memoize_by_aliases and (
        join_directives_by_aliases
        or leading_by_aliases
        or guc_values
        or row_by_aliases
        or parallel_by_alias
    ):
        return False

    constrained_joinrels = {
        frozenset(aliases)
        for aliases in join_directives_by_aliases
    }
    for leading_aliases, allowed_joinrels, is_flat in leading_joinrels:
        if any(
            not _leading_allows_joinrel(target_aliases, leading_aliases, allowed_joinrels, is_flat=is_flat)
            for target_aliases in constrained_joinrels
        ):
            return False

    if _guc_disables_method(guc_values, "enable_hashjoin") and _any_method(join_method_by_aliases, "HashJoin"):
        return False
    if _guc_disables_method(guc_values, "enable_mergejoin") and _any_method(join_method_by_aliases, "MergeJoin"):
        return False
    if _guc_disables_method(guc_values, "enable_nestloop") and _any_method(join_method_by_aliases, "NestLoop"):
        return False

    return True


def _body_alias_tokens(body: str) -> list[str]:
    return re.findall(r'"(?:[^"]|"")*"|[^\s(),]+', body)


def _diagnostic_body_tokens(body: str) -> list[str]:
    return re.findall(r'"(?:[^"]|"")*"|[^\s()"]+', body)


def _diagnostic_leading_tokens(body: str) -> list[str]:
    return re.findall(r'"(?:[^"]|"")*"|\(|\)|[^\s()"]+', body)


def _diagnostic_hint_parts(hint: str) -> tuple[str, str]:
    match = _HINT_HEAD_RE.match(hint)
    if not match:
        return "", ""
    raw_head = match.group(1)
    return _CANONICAL_HINT_HEADS.get(raw_head.casefold(), raw_head), match.group(2).strip()


def _hint_identifier_key(token: str) -> str:
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1].replace('""', '"')
    return token


def _parse_set_hint(body: str) -> tuple[str, str]:
    parts = body.split(None, 1)
    if len(parts) != 2:
        return "", ""
    return parts[0].lower(), parts[1].strip().lower()


def _guc_disables_method(guc_values: dict[str, set[str]], guc_name: str) -> bool:
    return "off" in guc_values.get(guc_name, set())


def _any_method(methods_by_aliases: dict[tuple[str, ...], set[str]], method: str) -> bool:
    return any(method in methods for methods in methods_by_aliases.values())


def _leading_order_key(body: str) -> str:
    _, normalized_body = parse_hint(f"Leading({body})")
    return normalized_body


def _leading_joinrel_sets(
    body: str,
) -> tuple[frozenset[str], frozenset[frozenset[str]], bool] | None:
    tokens = re.findall(r'"(?:[^"]|"")*"|\(|\)|[^\s(),]+', body)
    if not tokens:
        return None
    if "(" not in tokens and ")" not in tokens:
        aliases = [_hint_identifier_key(token) for token in tokens]
        if len(aliases) < 2 or len(set(aliases)) != len(aliases):
            return None
        joinrels = frozenset(frozenset(aliases[:size]) for size in range(2, len(aliases) + 1))
        return frozenset(aliases), joinrels, True

    position = 0

    def parse_node() -> tuple[frozenset[str], set[frozenset[str]]] | None:
        nonlocal position
        if position >= len(tokens):
            return None
        token = tokens[position]
        if token == ")":
            return None
        if token != "(":
            position += 1
            return frozenset({_hint_identifier_key(token)}), set()

        position += 1
        children: list[tuple[frozenset[str], set[frozenset[str]]]] = []
        while position < len(tokens) and tokens[position] != ")":
            child = parse_node()
            if child is None:
                return None
            children.append(child)
        if position >= len(tokens) or tokens[position] != ")" or len(children) != 2:
            return None
        position += 1
        left_aliases, left_joinrels = children[0]
        right_aliases, right_joinrels = children[1]
        if left_aliases & right_aliases:
            return None
        aliases = left_aliases | right_aliases
        return aliases, {*left_joinrels, *right_joinrels, aliases}

    root = parse_node()
    if root is None or position != len(tokens):
        return None
    aliases, joinrels = root
    if len(aliases) < 2:
        return None
    return aliases, frozenset(joinrels), False


def _leading_allows_joinrel(
    target_aliases: frozenset[str],
    leading_aliases: frozenset[str],
    allowed_joinrels: frozenset[frozenset[str]],
    *,
    is_flat: bool,
) -> bool:
    if target_aliases in allowed_joinrels:
        return True
    if leading_aliases <= target_aliases:
        return True
    if is_flat and len(target_aliases) <= len(leading_aliases):
        return False
    return target_aliases.isdisjoint(leading_aliases)
