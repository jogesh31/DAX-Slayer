r"""
Real DAX tokenizer for extracting object references (measures, columns,
tables) out of an expression — not a naive regex sweep.

Why not regex-only: patterns like r"\[([^\]]+)\]" match inside string literals,
across comments, and can't tell `Sales[Amount]` (a column ref) apart from
`SUM ( Sales[Amount] )`'s surrounding text reliably once expressions nest.
This scans the expression character-by-character, respecting:
  - string literals "..."  (with "" escape)
  - quoted identifiers '...'  (with '' escape)
  - line comments -- ...  and block comments /* ... */
and only then looks at the resulting token stream for `'Table'[Column]`,
`Table[Column]`, and bare `[Name]` patterns.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Token:
    kind: str  # "ident" | "quoted_ident" | "bracket" | "string" | "punct"
    value: str
    start: int
    end: int


def tokenize(expr: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]

        if c in " \t\r\n":
            i += 1
            continue

        # line comment
        if c == "-" and i + 1 < n and expr[i + 1] == "-":
            j = expr.find("\n", i)
            i = n if j == -1 else j + 1
            continue

        # block comment
        if c == "/" and i + 1 < n and expr[i + 1] == "*":
            j = expr.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue

        # string literal "..." with "" escape
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                if expr[j] == '"':
                    if j + 1 < n and expr[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(expr[j])
                j += 1
            tokens.append(Token("string", "".join(buf), i, j))
            i = j
            continue

        # quoted identifier '...' with '' escape
        if c == "'":
            j = i + 1
            buf = []
            while j < n:
                if expr[j] == "'":
                    if j + 1 < n and expr[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(expr[j])
                j += 1
            tokens.append(Token("quoted_ident", "".join(buf), i, j))
            i = j
            continue

        # bracket identifier [...] (no nested brackets in DAX identifiers)
        if c == "[":
            j = expr.find("]", i + 1)
            if j == -1:
                i += 1
                continue
            tokens.append(Token("bracket", expr[i + 1:j], i, j + 1))
            i = j + 1
            continue

        # bare identifier: letters/digits/underscore, must not start with digit
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (expr[j].isalnum() or expr[j] == "_" or expr[j] == "."):
                j += 1
            tokens.append(Token("ident", expr[i:j], i, j))
            i = j
            continue

        # everything else is punctuation (operators, parens, commas...)
        tokens.append(Token("punct", c, i, i + 1))
        i += 1

    return tokens


@dataclass
class ExtractedRef:
    ref_type: str  # "column" | "ambiguous"
    table: str | None  # None for ambiguous bare [Name] refs
    name: str


def extract_refs(expr: str) -> list[ExtractedRef]:
    """Walk the token stream and pull out table[column]/'table'[column]/[name] refs.

    Adjacency in the token stream (no gap between the identifier token and the
    following bracket token) is what distinguishes `Sales[Amount]` from
    `SUM ( Sales [Amount] )` written with a stray space some editors insert —
    Power BI's own formatter never inserts that space for a real column ref,
    so requiring immediate adjacency (same start/end offsets touching) avoids
    false positives like a table name mentioned in a comment right before an
    unrelated bracketed measure two tokens later.
    """
    tokens = tokenize(expr)
    refs: list[ExtractedRef] = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t.kind == "bracket":
            # look at previous token for immediate adjacency
            if i > 0:
                prev = tokens[i - 1]
                if prev.end == t.start and prev.kind in ("quoted_ident", "ident"):
                    refs.append(ExtractedRef("column", prev.value, t.value))
                    i += 1
                    continue
            refs.append(ExtractedRef("ambiguous", None, t.value))
        i += 1
    return refs
