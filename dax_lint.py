"""
Rule-based DAX best-practice/anti-pattern linter.

Deliberately not AI-based, for the same reason dax_explain.py and
dax_format.py aren't: this mirrors how the two respected tools in this
exact space actually work — Tabular Editor's "Best Practice Analyzer" and
SQLBI's public DAX rules are both fixed, deterministic pattern-matching
over the parsed expression, not a model "reading" the code and guessing.
That's not a compromise here, it's the correct design for a linter: the
same measure should get the same verdict every run, with no network call,
no API key, and no risk of a hallucinated rule that doesn't actually apply.

Every rule below is a well-documented, widely-cited DAX best practice
(DIVIDE over `/`, filtering by columns not FILTER(ALL(table)), not
table-qualifying measure references, VAR for repeated sub-expressions,
etc.) — this is a curated implementation of known guidance, not invented
rules.
"""
from __future__ import annotations

from dataclasses import dataclass

from dax_explain import _find_split, _split_call_args, _strip_parens
from dax_refs import Token, tokenize


@dataclass
class LintFinding:
    rule_id: str
    category: str
    severity: str  # "warning" | "info"
    title: str
    message: str
    start: int
    end: int


# ---------------------------------------------------------------------------
# Token-stream helpers shared by multiple rules.
# ---------------------------------------------------------------------------

def _all_calls(tokens: list[Token]) -> list[tuple[str, list[tuple[int, int]], int, int]]:
    """Every function-call site anywhere in the expression (any nesting
    depth), as (NAME, arg_ranges, open_paren_idx, close_paren_idx)."""
    calls = []
    n = len(tokens)
    for i, t in enumerate(tokens):
        if t.kind != "ident":
            continue
        if i + 1 >= n:
            continue
        nxt = tokens[i + 1]
        if nxt.kind == "punct" and nxt.value == "(" and nxt.start == t.end:
            args, close_idx = _split_call_args(tokens, i + 1)
            calls.append((t.value.upper(), args, i, close_idx))
    return calls


def _all_refs(tokens: list[Token]) -> list[tuple[str | None, str, int, int]]:
    """Every Table[Name] / 'Table'[Name] / bare [Name] reference site, as
    (table_or_None, name, start_offset, end_offset)."""
    refs = []
    n = len(tokens)
    for i, t in enumerate(tokens):
        if t.kind != "bracket":
            continue
        if i > 0:
            prev = tokens[i - 1]
            if prev.end == t.start and prev.kind in ("ident", "quoted_ident"):
                refs.append((prev.value, t.value, prev.start, t.end))
                continue
        refs.append((None, t.value, t.start, t.end))
    return refs


def _call_span(tokens: list[Token], name_idx: int, close_idx: int) -> tuple[int, int]:
    """Character offsets of a call, from its name's start to the closing
    paren. `name_idx` is the function-name token's own index — the same
    convention _all_calls returns, so callers pass its open_idx straight
    through with no adjustment."""
    return tokens[name_idx].start, tokens[close_idx].end


def _text(expr: str, tokens: list[Token], rng: tuple[int, int]) -> str:
    a, b = rng
    if a >= b:
        return ""
    return expr[tokens[a].start:tokens[b - 1].end].strip()


# ---------------------------------------------------------------------------
# Individual rules. Each takes (expr, tokens, ctx) and returns a list of
# LintFinding. `ctx` carries the optional model resolver so rules that need
# to know "is this a measure or a column" can degrade gracefully without one
# (e.g. linting a pasted expression with no connected model).
# ---------------------------------------------------------------------------

def _rule_use_divide(expr, tokens, ctx):
    findings = []
    for i, t in enumerate(tokens):
        if t.kind == "punct" and t.value == "/":
            findings.append(LintFinding(
                "DIV001", "Performance", "warning",
                "Use DIVIDE instead of /",
                "The / operator throws an error (and can blank out a whole visual) when the denominator is 0. "
                "DIVIDE(numerator, denominator, [alternate]) handles that case safely and is the standard pattern.",
                t.start, t.end,
            ))
    return findings


def _rule_filter_all_in_calculate(expr, tokens, ctx):
    findings = []
    calls = _all_calls(tokens)
    for name, args, open_idx, close_idx in calls:
        if name not in ("CALCULATE", "CALCULATETABLE"):
            continue
        for arg_rng in args[1:]:
            stripped = _strip_parens(tokens, arg_rng)
            a, b = stripped
            if a >= b or tokens[a].kind != "ident" or tokens[a].value.upper() != "FILTER":
                continue
            nxt = tokens[a + 1] if a + 1 < len(tokens) else None
            if not (nxt and nxt.kind == "punct" and nxt.value == "(" and nxt.start == tokens[a].end):
                continue
            filter_args, filter_close = _split_call_args(tokens, a + 1)
            if filter_close != b - 1 or len(filter_args) != 2:
                continue
            table_arg = _strip_parens(tokens, filter_args[0])
            ta, tb = table_arg
            is_bare_all = (
                tb - ta >= 3 and tokens[ta].kind == "ident" and tokens[ta].value.upper() == "ALL"
                and tokens[ta + 1].kind == "punct" and tokens[ta + 1].value == "(" and tokens[ta + 1].start == tokens[ta].end
            )
            if not is_bare_all:
                continue
            start, end = tokens[a].start, tokens[b - 1].end
            findings.append(LintFinding(
                "FLT001", "Performance", "warning",
                "Filter by columns, not FILTER(ALL(table))",
                f"{_text(expr, tokens, stripped)[:60]} scans every row of the table to re-check a condition CALCULATE could "
                "apply directly as a boolean filter argument — usually far more expensive. If the condition is a simple "
                "column comparison, pass it straight to CALCULATE instead of wrapping it in FILTER(ALL(...), ...).",
                start, end,
            ))
    return findings


def _rule_measure_table_prefix(expr, tokens, ctx):
    findings = []
    resolve_kind = ctx.get("resolve_kind")
    if not resolve_kind:
        return findings
    for table, name, start, end in _all_refs(tokens):
        if table is None:
            continue
        kind = resolve_kind(table, name)
        if kind == "measure":
            findings.append(LintFinding(
                "FMT001", "Formatting", "info",
                "Don't table-qualify measure references",
                f"{table}[{name}] is a measure, not a column — measures aren't really \"in\" a table. The DAX style "
                f"convention (and what Power BI's own formatter does) is to reference it as just [{name}].",
                start, end,
            ))
    return findings


def _rule_redundant_iferror_divide(expr, tokens, ctx):
    findings = []
    for name, args, open_idx, close_idx in _all_calls(tokens):
        if name != "IFERROR" or not args:
            continue
        inner = _strip_parens(tokens, args[0])
        a, b = inner
        if a < b and tokens[a].kind == "ident" and tokens[a].value.upper() == "DIVIDE":
            nxt = tokens[a + 1] if a + 1 < len(tokens) else None
            if nxt and nxt.kind == "punct" and nxt.value == "(" and nxt.start == tokens[a].end:
                start, end = _call_span(tokens, open_idx, close_idx)
                findings.append(LintFinding(
                    "ERR001", "Error Prevention", "info",
                    "IFERROR around DIVIDE is redundant",
                    "DIVIDE already returns blank (or its own alternate-result argument) instead of erroring on a "
                    "zero denominator, so wrapping it in IFERROR does nothing except hide *other* errors it wasn't meant to catch.",
                    start, end,
                ))
    return findings


def _rule_compare_to_blank(expr, tokens, ctx):
    findings = []
    calls = {(open_idx, close_idx) for name, _args, open_idx, close_idx in _all_calls(tokens) if name == "BLANK"}
    for open_idx, close_idx in calls:
        name_start = open_idx  # _all_calls already returns the ident's own index
        # look at the token immediately before the call for a comparison operator
        if name_start > 0:
            prev = tokens[name_start - 1]
            two_char = None
            if name_start > 1 and tokens[name_start - 2].end == prev.start:
                two_char = tokens[name_start - 2].value + prev.value
            op = None
            if two_char == "<>":
                op = "<>"
            elif prev.kind == "punct" and prev.value == "=":
                op = "="
            if op:
                start = tokens[name_start].start
                end = tokens[close_idx].end
                findings.append(LintFinding(
                    "ERR002", "Error Prevention", "info",
                    f"Use ISBLANK() instead of {op} BLANK()",
                    f"Comparing directly {op} BLANK() can behave surprisingly once blank-propagation rules kick in "
                    "(e.g. inside an iterator, or against a column that's never truly blank vs. filtered to nothing). "
                    "ISBLANK(<expr>) is the idiomatic, unambiguous check.",
                    start, end,
                ))
    return findings


def _rule_nested_calculate(expr, tokens, ctx):
    findings = []
    calc_calls = [(open_idx, close_idx) for name, _args, open_idx, close_idx in _all_calls(tokens) if name in ("CALCULATE", "CALCULATETABLE")]
    for open_idx, close_idx in calc_calls:
        depth = sum(1 for o2, c2 in calc_calls if (o2, c2) != (open_idx, close_idx) and o2 < open_idx and c2 > close_idx)
        if depth >= 2:
            start, end = _call_span(tokens, open_idx, close_idx)
            findings.append(LintFinding(
                "PERF001", "Maintainability", "warning",
                "Deeply nested CALCULATE",
                f"This CALCULATE is nested {depth + 1} levels deep. Each level changes filter context in a way that's easy "
                "to lose track of when reading the measure later — consider pulling intermediate results into VARs so the "
                "evaluation order is explicit instead of implied by nesting.",
                start, end,
            ))
    return findings


def _rule_repeated_subexpression(expr, tokens, ctx):
    findings = []
    seen: dict[str, tuple[int, int]] = {}
    for name, args, open_idx, close_idx in _all_calls(tokens):
        if name not in ("CALCULATE", "CALCULATETABLE", "FILTER", "SUMX", "AVERAGEX", "COUNTX", "MAXX", "MINX"):
            continue
        start, end = _call_span(tokens, open_idx, close_idx)
        if end - start < 20:  # skip trivial/short calls, not worth extracting
            continue
        normalized = " ".join(expr[start:end].split())
        if normalized in seen:
            findings.append(LintFinding(
                "MAINT001", "Maintainability", "info",
                "Repeated sub-expression — consider a VAR",
                f"\"{normalized[:50]}{'…' if len(normalized) > 50 else ''}\" appears more than once. DAX doesn't cache "
                "this automatically — it's evaluated fresh each time. Assigning it to a VAR once (at the top of the "
                "measure) computes it a single time and makes the duplication obvious to the next reader.",
                start, end,
            ))
        else:
            seen[normalized] = (start, end)
    return findings


RULES = [
    _rule_use_divide,
    _rule_filter_all_in_calculate,
    _rule_measure_table_prefix,
    _rule_redundant_iferror_divide,
    _rule_compare_to_blank,
    _rule_nested_calculate,
    _rule_repeated_subexpression,
]

RULE_CATALOG = [
    ("DIV001", "Performance", "Use DIVIDE instead of /"),
    ("FLT001", "Performance", "Filter by columns, not FILTER(ALL(table))"),
    ("FMT001", "Formatting", "Don't table-qualify measure references"),
    ("ERR001", "Error Prevention", "IFERROR around DIVIDE is redundant"),
    ("ERR002", "Error Prevention", "Use ISBLANK() instead of = BLANK()"),
    ("PERF001", "Maintainability", "Deeply nested CALCULATE"),
    ("MAINT001", "Maintainability", "Repeated sub-expression — consider a VAR"),
]


def lint_expression(expr: str, resolve_kind=None) -> list[LintFinding]:
    """resolve_kind(table, name) -> "measure" | "column" | None, used only by
    the table-qualified-measure-reference rule; omit it to lint a standalone
    expression with no connected model (that one rule just won't fire)."""
    expr = (expr or "").strip()
    if not expr:
        return []
    tokens = tokenize(expr)
    if not tokens:
        return []
    ctx = {"resolve_kind": resolve_kind}
    findings: list[LintFinding] = []
    for rule in RULES:
        try:
            findings.extend(rule(expr, tokens, ctx))
        except Exception:
            continue  # one rule misbehaving on an edge case shouldn't take down the rest
    findings.sort(key=lambda f: f.start)
    return findings
