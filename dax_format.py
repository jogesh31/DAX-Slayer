"""
Offline DAX pretty-printer — no network call, same spirit as daxformatter.com
but self-contained. Reuses the structural helpers already built for the
explainer (dax_explain.py) since "find a function call's top-level argument
ranges" is exactly the same problem beautifying and explaining both need.

Rule of thumb, matching how most hand-written/DAX-Studio-formatted measures
look: a function call with 0 or 1 short, simple arguments stays on one
line; anything with 2+ arguments, or a single argument that's itself a
nested call, breaks onto its own indented lines with the closing paren
dedented back to the call's own level.
"""
from __future__ import annotations

from dax_explain import _match_call, _strip_parens, _text
from dax_refs import tokenize

INDENT = 4
INLINE_WIDTH = 60  # a single-arg call renders inline if it fits under this


def _format_range(expr: str, tokens, rng: tuple[int, int], indent: int) -> str:
    rng = _strip_parens(tokens, rng)
    a, b = rng
    if a >= b:
        return ""

    call = _match_call(tokens, rng)
    if not call:
        # Not a pure function call (a reference, literal, comparison,
        # arithmetic expression, ...) — DAX measures read fine with these
        # left as single lines; only call arguments get broken out.
        return _text(expr, tokens, rng)

    name, args = call
    if not args:
        return f"{name}()"

    if len(args) == 1:
        inline_arg = _format_range(expr, tokens, args[0], indent)
        if "\n" not in inline_arg and len(name) + len(inline_arg) + 2 <= INLINE_WIDTH:
            return f"{name}({inline_arg})"

    pad_in = " " * (indent + INDENT)
    pad_out = " " * indent
    lines = [f"{name}("]
    for i, arg_rng in enumerate(args):
        arg_text = _format_range(expr, tokens, arg_rng, indent + INDENT)
        comma = "," if i < len(args) - 1 else ""
        # a nested multi-line arg already carries its own internal indent
        # from the recursive call; only the first line needs the pad here
        arg_lines = arg_text.split("\n")
        arg_lines[0] = pad_in + arg_lines[0]
        lines.append("\n".join(arg_lines) + comma)
    lines.append(f"{pad_out})")
    return "\n".join(lines)


def format_expression(expr: str) -> str:
    expr = (expr or "").strip()
    if not expr:
        return expr
    tokens = tokenize(expr)
    if not tokens:
        return expr
    try:
        return _format_range(expr, tokens, (0, len(tokens)), 0)
    except Exception:
        return expr  # formatting is a convenience — never let it break the DAX box
