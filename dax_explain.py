"""
Rule-based, offline "what does this measure actually calculate" explainer.

Deliberately NOT an LLM call: no network round-trip, no API key, no risk of
sending a customer's business logic to a third party. Instead this parses
the expression's real structure (reusing dax_refs.tokenize) — function
calls, column/measure references, comparisons, set literals, arithmetic —
and renders it as English recursively, to full depth, resolving every
`Table[Column]` and bare `[Measure]` reference against the actual semantic
model (via a resolver callback built from the dependency graph) rather than
just echoing raw DAX text back at the reader. That resolution against the
live model — knowing a bracketed name is really the "Total Teachers
Completed" measure vs. a column on a specific table — is the "use the whole
semantic model" part; the sentence construction below is what turns that
into something a non-DAX-reader can actually follow.
"""
from __future__ import annotations

from dataclasses import dataclass

from dax_functions import get_function
from dax_refs import Token, tokenize

MAX_DEPTH = 6
Resolver = "Callable[[str | None, str], str]"  # (table_or_None, name) -> friendly phrase


@dataclass
class FunctionUse:
    name: str
    start: int
    end: int


def find_function_calls(expr: str) -> list[FunctionUse]:
    """Every `IDENT(` in the expression where IDENT is a known DAX function
    name — used by the frontend to turn function names in the DAX box into
    clickable spans, independent of whether the structural walk below
    produces a template sentence for that particular call."""
    tokens = tokenize(expr)
    uses: list[FunctionUse] = []
    for i, t in enumerate(tokens):
        if t.kind != "ident":
            continue
        if i + 1 >= len(tokens):
            continue
        nxt = tokens[i + 1]
        if nxt.kind == "punct" and nxt.value == "(" and nxt.start == t.end:
            info = get_function(t.value)
            if info:
                uses.append(FunctionUse(info.name, t.start, t.end))
    return uses


# ---------------------------------------------------------------------------
# Token-range helpers. A "range" is a (start_idx, end_idx) pair of indices
# into the token list, half-open like Python slicing.
# ---------------------------------------------------------------------------

def _strip_parens(tokens: list[Token], rng: tuple[int, int]) -> tuple[int, int]:
    """Peel off redundant outer parens: (X) -> X, repeatedly."""
    a, b = rng
    while b - a >= 2 and tokens[a].kind == "punct" and tokens[a].value == "(":
        depth = 1
        i = a + 1
        while i < b and depth > 0:
            if tokens[i].kind == "punct" and tokens[i].value == "(":
                depth += 1
            elif tokens[i].kind == "punct" and tokens[i].value == ")":
                depth -= 1
            i += 1
        if depth == 0 and i == b:  # the matching ')' is the very last token — genuinely wraps the whole range
            a, b = a + 1, b - 1
        else:
            break
    return (a, b)


def _split_call_args(tokens: list[Token], open_idx: int) -> tuple[list[tuple[int, int]], int]:
    """tokens[open_idx] must be the '(' of a call. Returns (top-level
    argument ranges, index of the matching ')')."""
    depth = 1
    i = open_idx + 1
    seg_start = i
    args: list[tuple[int, int]] = []
    n = len(tokens)
    while i < n and depth > 0:
        t = tokens[i]
        if t.kind == "punct" and t.value in "({":
            depth += 1
        elif t.kind == "punct" and t.value in ")}":
            depth -= 1
            if depth == 0:
                args.append((seg_start, i))
                break
        elif t.kind == "punct" and t.value == "," and depth == 1:
            args.append((seg_start, i))
            seg_start = i + 1
        i += 1
    return args, i


def _text(expr: str, tokens: list[Token], rng: tuple[int, int]) -> str:
    a, b = rng
    if a >= b:
        return ""
    return expr[tokens[a].start:tokens[b - 1].end].strip()


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _match_ref(tokens: list[Token], rng: tuple[int, int]) -> tuple[str | None, str] | None:
    """If this range is exactly one column/measure reference — `Table[Col]`,
    `'Table'[Col]`, or a bare `[Name]` — return (table_or_None, name)."""
    a, b = rng
    if b - a == 1 and tokens[a].kind == "bracket":
        return (None, tokens[a].value)
    if b - a == 2 and tokens[a].kind in ("ident", "quoted_ident") and tokens[a + 1].kind == "bracket" \
            and tokens[a].end == tokens[a + 1].start:
        return (tokens[a].value, tokens[a + 1].value)
    return None


def _match_call(tokens: list[Token], rng: tuple[int, int]) -> tuple[str, list[tuple[int, int]]] | None:
    """If this range is exactly one function call spanning end-to-end,
    return (FUNCTION_NAME, arg_ranges)."""
    a, b = rng
    if b - a < 3:
        return None
    if tokens[a].kind != "ident":
        return None
    if not (tokens[a + 1].kind == "punct" and tokens[a + 1].value == "(" and tokens[a + 1].start == tokens[a].end):
        return None
    args, close_idx = _split_call_args(tokens, a + 1)
    if close_idx != b - 1:
        return None
    return tokens[a].value.upper(), args


_OP_LEVELS = [
    (["||", "OR"], "logical_or"),
    (["&&", "AND"], "logical_and"),
    (["=", "<>", ">", "<", ">=", "<=", "IN"], "comparison"),
    (["&"], "concatenation"),  # DAX's text-join operator — binds looser than +/- so "a" & b + 1 reads as "a" & (b+1)
    (["+", "-"], "additive"),
    (["*", "/"], "multiplicative"),
]

_COMPARISON_PHRASE = {
    "=": "is", "<>": "is not", ">": "is more than", "<": "is less than",
    ">=": "is at least", "<=": "is at most", "IN": "is one of",
}
_ARITH_PHRASE = {"+": "plus", "-": "minus", "*": "multiplied by", "/": "divided by"}


def _op_token(tokens: list[Token], i: int) -> tuple[str, int] | None:
    """Does a (possibly two-token) operator start at tokens[i]? Returns
    (operator_string, number_of_tokens_consumed)."""
    t = tokens[i]
    if t.kind == "ident" and t.value.upper() in ("IN", "AND", "OR"):
        return (t.value.upper(), 1)
    if t.kind != "punct":
        return None
    if i + 1 < len(tokens) and tokens[i + 1].kind == "punct" and tokens[i + 1].start == t.end:
        two = t.value + tokens[i + 1].value
        if two in ("<>", ">=", "<=", "&&", "||"):
            return (two, 2)
    if t.value in ("=", ">", "<", "+", "-", "*", "/", "&"):
        return (t.value, 1)
    return None


def _find_split(tokens: list[Token], rng: tuple[int, int], symbols: list[str]) -> tuple[int, str, int] | None:
    """Scan rng at bracket-depth 0 for the LAST occurrence of an operator in
    `symbols` (rightmost split keeps left-associative chains reading
    naturally when the left side is recursively split again)."""
    a, b = rng
    depth = 0
    found: tuple[int, str, int] | None = None
    i = a
    while i < b:
        t = tokens[i]
        if t.kind == "punct" and t.value in "({":
            depth += 1
            i += 1
            continue
        if t.kind == "punct" and t.value in ")}":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            op = _op_token(tokens, i)
            if op:
                if op[0] in symbols:
                    found = (i, op[0], op[1])
                # Always skip the full matched width, even for operators we're not
                # collecting here — otherwise the second token of a two-token op
                # like "<>" gets rescanned on its own as a bogus lone ">" match,
                # which (being later) would win as the "last occurrence" and
                # corrupt the split.
                i += op[1]
                continue
        i += 1
    return found


def _describe_set(expr: str, tokens: list[Token], rng: tuple[int, int]) -> str:
    """{"A", "B", "C"} -> A, B, C — splits on top-level commas within the braces."""
    a, b = rng
    if not (a < b and tokens[a].kind == "punct" and tokens[a].value == "{" and tokens[b - 1].kind == "punct" and tokens[b - 1].value == "}"):
        return _short(_text(expr, tokens, rng))
    depth = 0
    seg_start = a + 1
    parts = []
    for i in range(a + 1, b - 1):
        t = tokens[i]
        if t.kind == "punct" and t.value in "({":
            depth += 1
        elif t.kind == "punct" and t.value in ")}":
            depth -= 1
        elif t.kind == "punct" and t.value == "," and depth == 0:
            parts.append((seg_start, i))
            seg_start = i + 1
    parts.append((seg_start, b - 1))
    rendered = [_short(_text(expr, tokens, p)) for p in parts if p[1] > p[0]]
    return ", ".join(rendered)


def describe(expr: str, tokens: list[Token], rng: tuple[int, int], resolve, depth: int = 0) -> str:
    rng = _strip_parens(tokens, rng)
    a, b = rng
    if a >= b:
        return ""
    if depth >= MAX_DEPTH:
        return _short(_text(expr, tokens, rng))

    # a lone reference — the single most impactful place to use the model:
    # turn "VW_TPD[COURSE_ID]" into a phrase that names what it actually is
    ref = _match_ref(tokens, rng)
    if ref is not None:
        return resolve(ref[0], ref[1])

    # a set literal {...}
    if tokens[a].kind == "punct" and tokens[a].value == "{":
        return _describe_set(expr, tokens, rng)

    # a single literal token (string, number-as-punct-run, TRUE/FALSE, ident)
    if b - a == 1:
        t = tokens[a]
        if t.kind == "string":
            return f'"{t.value}"'
        return t.value

    # binary operators, lowest precedence first, split at the last top-level occurrence
    for symbols, _kind in _OP_LEVELS:
        split = _find_split(tokens, rng, symbols)
        if split:
            op_idx, op, width = split
            lhs = describe(expr, tokens, (a, op_idx), resolve, depth + 1)
            rhs_rng = (op_idx + width, b)
            if op in _COMPARISON_PHRASE:
                rhs = _describe_set(expr, tokens, rhs_rng) if (rhs_rng[1] > rhs_rng[0] and tokens[rhs_rng[0]].kind == "punct" and tokens[rhs_rng[0]].value == "{") \
                    else describe(expr, tokens, rhs_rng, resolve, depth + 1)
                return f"{lhs} {_COMPARISON_PHRASE[op]} {rhs}"
            if op in ("AND", "&&"):
                rhs = describe(expr, tokens, rhs_rng, resolve, depth + 1)
                return f"{lhs} and {rhs}"
            if op in ("OR", "||"):
                rhs = describe(expr, tokens, rhs_rng, resolve, depth + 1)
                return f"{lhs} or {rhs}"
            if op == "&":
                rhs = describe(expr, tokens, rhs_rng, resolve, depth + 1)
                return f"{lhs} joined with {rhs}"
            if op in _ARITH_PHRASE:
                rhs = describe(expr, tokens, rhs_rng, resolve, depth + 1)
                return f"{lhs} {_ARITH_PHRASE[op]} {rhs}"

    # a function call — recurse fully via templates, no truncation
    call = _match_call(tokens, rng)
    if call:
        name, args = call
        template = _TEMPLATES.get(name)
        if template:
            try:
                result = template(expr, tokens, args, resolve, depth)
                if result:
                    return result
            except Exception:
                pass
        info = get_function(name)
        arg_texts = [describe(expr, tokens, r, resolve, depth + 1) for r in args] if args else []
        if info:
            return f"{name.lower()}({', '.join(arg_texts)})" if arg_texts else f"{name.lower()}()"

    return _short(_text(expr, tokens, rng))


# ---------------------------------------------------------------------------
# Per-function templates. Each takes (expr, tokens, arg_ranges, resolve,
# depth) and returns a full English sentence, recursing into `describe` for
# every sub-argument so nested calls are expanded to full depth rather than
# stubbed out.
# ---------------------------------------------------------------------------

def _d(expr, tokens, rng, resolve, depth):
    # Strip any trailing period a nested template added for its own
    # (potentially top-level) case — when spliced into a parent sentence a
    # mid-sentence full stop reads as a typo, and only the outermost call
    # (in explain_expression) should terminate the sentence.
    return describe(expr, tokens, rng, resolve, depth + 1).rstrip(". ")


def _t_divide(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    num = _d(expr, tokens, args[0], resolve, depth)
    den = _d(expr, tokens, args[1], resolve, depth)
    if len(args) > 2:
        alt = _d(expr, tokens, args[2], resolve, depth)
        return f"{num}, divided by {den} — falling back to {alt} if that division would be by zero."
    return f"{num}, divided by {den} (returns blank instead of an error if that's a division by zero)."


def _t_calculate(expr, tokens, args, resolve, depth):
    if not args:
        return None
    base = _d(expr, tokens, args[0], resolve, depth)
    if len(args) == 1:
        return f"{base}."
    filters = [_d(expr, tokens, r, resolve, depth) for r in args[1:]]
    if len(filters) == 1:
        return f"{base}, but only counting rows where {filters[0]}."
    joined = "; ".join(filters)
    return f"{base}, but only counting rows where: {joined}."


def _t_calculatetable(expr, tokens, args, resolve, depth):
    if not args:
        return None
    base = _d(expr, tokens, args[0], resolve, depth)
    filters = [_d(expr, tokens, r, resolve, depth) for r in args[1:]]
    if filters:
        return f"The rows of {base}, filtered to where: {'; '.join(filters)}."
    return f"The rows of {base}."


def _t_agg(verb):
    def fn(expr, tokens, args, resolve, depth):
        if not args:
            return None
        col = _d(expr, tokens, args[0], resolve, depth)
        return f"the {verb} of {col}"
    return fn


def _t_aggx(verb):
    def fn(expr, tokens, args, resolve, depth):
        if len(args) < 2:
            return None
        table = _d(expr, tokens, args[0], resolve, depth)
        expr_text = _d(expr, tokens, args[1], resolve, depth)
        return f"for every row of {table}, work out {expr_text} — then take the {verb} of those results"
    return fn


def _t_if(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    cond = _d(expr, tokens, args[0], resolve, depth)
    then = _d(expr, tokens, args[1], resolve, depth)
    if len(args) > 2:
        els = _d(expr, tokens, args[2], resolve, depth)
        return f"if {cond}, this is {then} — otherwise it's {els}"
    return f"if {cond}, this is {then} — otherwise blank"


def _t_switch(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    switch_on = _d(expr, tokens, args[0], resolve, depth)
    pairs = []
    i = 1
    while i + 1 < len(args):
        val = _d(expr, tokens, args[i], resolve, depth)
        res = _d(expr, tokens, args[i + 1], resolve, depth)
        pairs.append(f"{val} → {res}")
        i += 2
    tail = f"; otherwise {_d(expr, tokens, args[i], resolve, depth)}" if i < len(args) else ""
    on_phrase = "checks which case applies" if switch_on.upper() == "TRUE()" or switch_on.upper() == "TRUE" else f"checks {switch_on} against"
    return f"{on_phrase}: {'; '.join(pairs)}{tail}"


def _t_filter(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    table = _d(expr, tokens, args[0], resolve, depth)
    cond = _d(expr, tokens, args[1], resolve, depth)
    return f"just the rows of {table} where {cond}"


def _t_all(expr, tokens, args, resolve, depth):
    if not args:
        return "every row, ignoring whatever filters are currently applied"
    targets = [_d(expr, tokens, r, resolve, depth) for r in args]
    return f"every row of {', '.join(targets)}, ignoring whatever the user has filtered or sliced by there"


def _t_allselected(expr, tokens, args, resolve, depth):
    if not args:
        return "every row visible within this visual, ignoring its own row/column breakdown but respecting outside filters like slicers"
    targets = [_d(expr, tokens, r, resolve, depth) for r in args]
    return f"{', '.join(targets)}, ignoring the visual's own row/column breakdown but respecting outside filters like slicers"


def _t_removefilters(expr, tokens, args, resolve, depth):
    if not args:
        return "removing every filter currently applied"
    targets = [_d(expr, tokens, r, resolve, depth) for r in args]
    return f"removing whatever filters are applied to {', '.join(targets)}"


def _t_allexcept(expr, tokens, args, resolve, depth):
    if not args:
        return None
    table = _d(expr, tokens, args[0], resolve, depth)
    keep = [_d(expr, tokens, r, resolve, depth) for r in args[1:]]
    keep_text = ", ".join(keep) if keep else "nothing"
    return f"every row of {table}, clearing all filters except whatever's applied on {keep_text}"


def _t_related(expr, tokens, args, resolve, depth):
    col = _d(expr, tokens, args[0], resolve, depth) if args else "a related column"
    return f"the matching {col}, looked up through the model's relationship"


def _t_relatedtable(expr, tokens, args, resolve, depth):
    table = _d(expr, tokens, args[0], resolve, depth) if args else "a related table"
    return f"all rows of {table} related to the current row"


def _t_values(expr, tokens, args, resolve, depth):
    target = _d(expr, tokens, args[0], resolve, depth) if args else "a column"
    return f"whatever distinct values of {target} are currently visible"


def _t_distinct(expr, tokens, args, resolve, depth):
    target = _d(expr, tokens, args[0], resolve, depth) if args else "a column"
    return f"the distinct values of {target}"


def _t_totalytd(expr, tokens, args, resolve, depth):
    base = _d(expr, tokens, args[0], resolve, depth) if args else "a value"
    return f"a running year-to-date total of {base}, accumulated from the start of the year up to the latest date in view"


def _t_totalqtd(expr, tokens, args, resolve, depth):
    base = _d(expr, tokens, args[0], resolve, depth) if args else "a value"
    return f"a running quarter-to-date total of {base}"


def _t_totalmtd(expr, tokens, args, resolve, depth):
    base = _d(expr, tokens, args[0], resolve, depth) if args else "a value"
    return f"a running month-to-date total of {base}"


def _t_sameperiodlastyear(expr, tokens, args, resolve, depth):
    return "the same dates, shifted back exactly one year — used to compare against last year"


def _t_previousyear(expr, tokens, args, resolve, depth):
    return "the entire previous year"


def _t_dateadd(expr, tokens, args, resolve, depth):
    if len(args) < 3:
        return None
    interval = _d(expr, tokens, args[1], resolve, depth)
    unit = _d(expr, tokens, args[2], resolve, depth)
    return f"the dates shifted by {interval} {unit.lower()}(s)"


def _t_rankx(expr, tokens, args, resolve, depth):
    if not args:
        return None
    table = _d(expr, tokens, args[0], resolve, depth)
    return f"this row's rank compared to every row of {table}"


def _t_coalesce(expr, tokens, args, resolve, depth):
    parts = [_d(expr, tokens, r, resolve, depth) for r in args]
    return f"the first non-blank value among: {', '.join(parts)}"


def _t_selectedvalue(expr, tokens, args, resolve, depth):
    col = _d(expr, tokens, args[0], resolve, depth) if args else "a column"
    fallback = f", or {_d(expr, tokens, args[1], resolve, depth)} if more than one value is selected" if len(args) > 1 else ""
    return f"the single selected value of {col}{fallback}"


def _t_countrows(expr, tokens, args, resolve, depth):
    table = _d(expr, tokens, args[0], resolve, depth) if args else "a table"
    return f"the number of rows in {table}"


def _t_isblank(expr, tokens, args, resolve, depth):
    val = _d(expr, tokens, args[0], resolve, depth) if args else "a value"
    return f"whether {val} is blank"


def _t_iferror(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    val = _d(expr, tokens, args[0], resolve, depth)
    fallback = _d(expr, tokens, args[1], resolve, depth)
    return f"{val} — or {fallback} if that would error"


def _t_userelationship(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    c1 = _d(expr, tokens, args[0], resolve, depth)
    c2 = _d(expr, tokens, args[1], resolve, depth)
    return f"using the (otherwise inactive) relationship between {c1} and {c2} for this calculation"


def _t_keepfilters(expr, tokens, args, resolve, depth):
    inner = _d(expr, tokens, args[0], resolve, depth) if args else "a filter"
    return f"{inner} (intersected with whatever's already filtered, not replacing it)"


def _t_concatenatex(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    delim = f", separated by {_d(expr, tokens, args[2], resolve, depth)}" if len(args) > 2 else ""

    # The extremely common idiom CONCATENATEX(VALUES(col), col, delim) — "every
    # distinct value of this column, joined together" — reads far better
    # collapsed into one phrase than spelled out as "for every row of the
    # distinct values of X, work out X, then join...".
    table_call = _match_call(tokens, _strip_parens(tokens, args[0]))
    if table_call and table_call[0] == "VALUES" and len(table_call[1]) == 1:
        values_ref = _match_ref(tokens, _strip_parens(tokens, table_call[1][0]))
        expr_ref = _match_ref(tokens, _strip_parens(tokens, args[1]))
        if values_ref and values_ref == expr_ref:
            col = resolve(*values_ref)
            return f"every distinct value of {col} currently visible, joined together{delim}"

    table = _d(expr, tokens, args[0], resolve, depth)
    expr_text = _d(expr, tokens, args[1], resolve, depth)
    return f"for every row of {table}, works out {expr_text}, then joins all of those into one piece of text{delim}"


def _t_concatenate(expr, tokens, args, resolve, depth):
    if len(args) < 2:
        return None
    a1 = _d(expr, tokens, args[0], resolve, depth)
    a2 = _d(expr, tokens, args[1], resolve, depth)
    return f"{a1} joined with {a2}"


def _t_format(expr, tokens, args, resolve, depth):
    if not args:
        return None
    val = _d(expr, tokens, args[0], resolve, depth)
    fmt = f" as {_d(expr, tokens, args[1], resolve, depth)}" if len(args) > 1 else ""
    return f"{val}, formatted as text{fmt}"


def _t_selectedmeasure(expr, tokens, args, resolve, depth):
    return "whichever measure is currently selected (e.g. via a field parameter)"


_TEMPLATES = {
    "DIVIDE": _t_divide,
    "CALCULATE": _t_calculate,
    "CALCULATETABLE": _t_calculatetable,
    "SUM": _t_agg("sum"),
    "AVERAGE": _t_agg("average"),
    "MIN": _t_agg("minimum"),
    "MAX": _t_agg("maximum"),
    "COUNT": _t_agg("count"),
    "COUNTA": _t_agg("count"),
    "COUNTROWS": _t_countrows,
    "DISTINCTCOUNT": _t_agg("distinct count"),
    "MEDIAN": _t_agg("median"),
    "SUMX": _t_aggx("sum"),
    "AVERAGEX": _t_aggx("average"),
    "MINX": _t_aggx("minimum"),
    "MAXX": _t_aggx("maximum"),
    "COUNTX": _t_aggx("count"),
    "IF": _t_if,
    "SWITCH": _t_switch,
    "FILTER": _t_filter,
    "ALL": _t_all,
    "ALLNOBLANKROW": _t_all,
    "ALLSELECTED": _t_allselected,
    "ALLEXCEPT": _t_allexcept,
    "REMOVEFILTERS": _t_removefilters,
    "RELATED": _t_related,
    "RELATEDTABLE": _t_relatedtable,
    "VALUES": _t_values,
    "DISTINCT": _t_distinct,
    "TOTALYTD": _t_totalytd,
    "TOTALQTD": _t_totalqtd,
    "TOTALMTD": _t_totalmtd,
    "SAMEPERIODLASTYEAR": _t_sameperiodlastyear,
    "PREVIOUSYEAR": _t_previousyear,
    "PREVIOUSMONTH": _t_previousyear,
    "PREVIOUSQUARTER": _t_previousyear,
    "DATEADD": _t_dateadd,
    "RANKX": _t_rankx,
    "COALESCE": _t_coalesce,
    "SELECTEDVALUE": _t_selectedvalue,
    "ISBLANK": _t_isblank,
    "IFERROR": _t_iferror,
    "USERELATIONSHIP": _t_userelationship,
    "KEEPFILTERS": _t_keepfilters,
    "CONCATENATEX": _t_concatenatex,
    "CONCATENATE": _t_concatenate,
    "FORMAT": _t_format,
    "SELECTEDMEASURE": _t_selectedmeasure,
}


def _default_resolver(table: str | None, name: str) -> str:
    return f"{table}[{name}]" if table else f"[{name}]"


def _flatten_concat_chain(tokens: list[Token], rng: tuple[int, int]) -> list[tuple[int, int]]:
    """Splits a left-associative `&` chain into its ordered segments, e.g.
    `"a" & b & "c"` -> [range("a"), range(b), range("c")]. Repeatedly peels
    the rightmost top-level `&` off, so it correctly unwinds regardless of
    how deep the chain is."""
    parts: list[tuple[int, int]] = []
    cur = _strip_parens(tokens, rng)
    while True:
        split = _find_split(tokens, cur, ["&"])
        if not split:
            parts.append(cur)
            break
        op_idx, _op, width = split
        parts.append((op_idx + width, cur[1]))
        cur = (cur[0], op_idx)
    parts.reverse()
    return parts


def explain_expression(expr: str, resolve=None) -> dict:
    """resolve(table_or_None, name) -> friendly phrase for a column/measure
    reference; pass one built from the live semantic model to turn raw
    bracket references into readable names, e.g. "the Total Teachers
    Completed measure" instead of "Measure Table[Total Teachers Completed]".
    """
    expr = (expr or "").strip()
    resolve = resolve or _default_resolver
    if not expr:
        return {"summary": "", "functionsUsed": []}

    uses = find_function_calls(expr)
    functions_used = [{"name": u.name, "start": u.start, "end": u.end} for u in uses]

    tokens = tokenize(expr)

    # Measures built by concatenating 3+ pieces (dynamic titles, labels,
    # tooltips — extremely common) read far better as a numbered list than
    # as one long "X joined with Y joined with Z" run-on sentence.
    chain_parts = _flatten_concat_chain(tokens, (0, len(tokens)))
    if len(chain_parts) >= 3:
        lines = ["Builds a text string by joining these parts, in order:"]
        for i, seg in enumerate(chain_parts):
            piece = describe(expr, tokens, seg, resolve, 0)
            lines.append(f"{i + 1}. {piece}")
        summary = "\n".join(lines)
        return {"summary": summary, "functionsUsed": functions_used}

    summary = describe(expr, tokens, (0, len(tokens)), resolve, 0)
    if summary:
        summary = summary[0].upper() + summary[1:]
        if not summary.endswith((".", "!", "?")):
            summary += "."
    return {"summary": summary, "functionsUsed": functions_used}
