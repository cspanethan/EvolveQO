import re
from collections.abc import Iterable

INDEX_METHODS = ("IndexScan", "IndexOnlyScan", "BitmapScan")
REGEXP_METHODS = tuple(method + "Regexp" for method in INDEX_METHODS)
POSITIVE_SCAN_METHODS = ("SeqScan", "TidScan", *INDEX_METHODS)
NEGATIVE_SCAN_METHODS = (
    "NoSeqScan",
    "NoTidScan",
    "NoIndexScan",
    "NoIndexOnlyScan",
    "NoBitmapScan",
)
SCAN_METHODS = (*POSITIVE_SCAN_METHODS, *NEGATIVE_SCAN_METHODS)
POSITIVE_JOIN_METHODS = ("HashJoin", "MergeJoin", "NestLoop")
NEGATIVE_JOIN_METHODS = tuple("No" + method for method in POSITIVE_JOIN_METHODS)
JOIN_METHODS = (*POSITIVE_JOIN_METHODS, *NEGATIVE_JOIN_METHODS)
OPTIMIZER_HINT_HEADS = frozenset((*SCAN_METHODS, *REGEXP_METHODS, *JOIN_METHODS, "DisableIndex", "Leading", "Rows"))

FIXED_PLANNER_SETTINGS = {"geqo": "off"}


def require_search_hints(hints: Iterable[str]) -> None:
    for hint in hints:
        match = re.match(r"\s*(\w+)\s*\(", hint)
        if match is None or match.group(1) not in OPTIMIZER_HINT_HEADS or not _single_hint(hint, match.end()):
            raise ValueError(f"Hint outside optimizer search scope: {hint!r}")


def _single_hint(hint: str, body_start: int) -> bool:
    depth, quoted = 1, False
    for index in range(body_start, len(hint)):
        char = hint[index]
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return not hint[index + 1 :].strip()
    return False
