"""
Builds the full dependency graph for a Power BI semantic model: nodes are
tables/columns/measures, edges are "A's DAX references B" (for measures)
plus modeled relationships (for tables). Also classifies usage level per
object by combining DAX cross-references with report-JSON usage (report_scan).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dax_refs import extract_refs
from report_scan import ReportUsage, scan_report_folder, text_fallback_used


def _clean(val):
    """pandas gives back NaN (a float) for empty DataFrame cells, not None —
    and NaN is truthy in Python, so `val or None` silently lets it through.
    A bare NaN then serializes to a literal `NaN` token in the JSON response,
    which is invalid per spec and makes the browser's JSON.parse throw."""
    if val is None:
        return None
    try:
        if val != val:  # NaN is the only value that isn't equal to itself
            return None
    except Exception:
        pass
    return val or None


@dataclass
class Node:
    id: str  # "table" or "table[name]"
    node_type: str  # "table" | "column" | "measure"
    table: str
    name: str
    expression: str | None = None
    is_hidden: bool = False
    is_calculated: bool = False  # column only: True for DAX-defined columns, False for plain source-data columns
    display_folder: str | None = None  # measure only: current Display Folder grouping in Power BI's field list


@dataclass
class Edge:
    source: str  # id of the referencing object
    target: str  # id of the referenced object


@dataclass
class UsageInfo:
    used_by_measures: list[str] = field(default_factory=list)
    used_in_report: bool = False
    report_sources: list[str] = field(default_factory=list)
    level: str = "unused"  # "unused" | "used-by-measure-only" | "used-in-report"


@dataclass
class ModelGraph:
    nodes: dict[str, Node]
    edges: list[Edge]
    usage: dict[str, UsageInfo]
    unresolved_refs: list[dict]  # refs the tokenizer found that couldn't be matched to a real object
    circular: list[list[str]]
    report_files_scanned: int = 0


def _resolve_ambiguous(name: str, owner_table: str, measure_names: set[str], columns_by_table: dict[str, set[str]]) -> tuple[str, str | None]:
    """Bare `[Name]` could be a measure (global namespace) or a column of the
    expression's own table. Measures take precedence — that's DAX's own
    resolution order when a bare bracket ref is ambiguous."""
    if name in measure_names:
        return "measure", None
    if name in columns_by_table.get(owner_table, set()):
        return "column", owner_table
    return "unknown", None


def build_graph(tables_df, columns_df, measures_df, report_folder: str | None = None) -> ModelGraph:
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    unresolved_refs: list[dict] = []

    measure_names = set(measures_df["Name"]) if not measures_df.empty else set()
    columns_by_table: dict[str, set[str]] = {}

    for _, t in tables_df.iterrows():
        tid = f"table::{t['Name']}"
        nodes[tid] = Node(id=tid, node_type="table", table=t["Name"], name=t["Name"], is_hidden=bool(t.get("IsHidden")))

    for _, c in columns_df.iterrows():
        cid = f"{c['Table']}[{c['Name']}]"
        col_type = _clean(c.get("Type")) or ""
        nodes[cid] = Node(
            id=cid, node_type="column", table=c["Table"], name=c["Name"],
            expression=_clean(c.get("Expression")),
            is_hidden=bool(c.get("IsHidden")),
            is_calculated=col_type in ("Calculated", "CalculatedTableColumn"),
        )
        columns_by_table.setdefault(c["Table"], set()).add(c["Name"])

    for _, m in measures_df.iterrows():
        mid = f"{m['Table']}[{m['Name']}]"
        expr = _clean(m.get("Expression")) or ""
        nodes[mid] = Node(
            id=mid, node_type="measure", table=m["Table"], name=m["Name"], expression=expr,
            is_hidden=bool(m.get("IsHidden")), display_folder=_clean(m.get("DisplayFolder")),
        )

    # ---- dependency edges from DAX expressions (measures only carry logic) ----
    for _, m in measures_df.iterrows():
        mid = f"{m['Table']}[{m['Name']}]"
        expr = m.get("Expression") or ""
        for ref in extract_refs(expr):
            if ref.ref_type == "column":
                table_name = ref.table
                # could be a measure written as Table[MeasureName]-looking bracket too;
                # DAX doesn't actually allow qualifying a measure with a table, so
                # a real Table[Name] pair is always a column reference.
                target_id = f"{table_name}[{ref.name}]"
                if target_id in nodes:
                    edges.append(Edge(source=mid, target=target_id))
                else:
                    unresolved_refs.append({"from": mid, "raw": f"{table_name}[{ref.name}]"})
            else:
                kind, owner = _resolve_ambiguous(ref.name, m["Table"], measure_names, columns_by_table)
                if kind == "measure":
                    # global measure name -> find its node (any table)
                    matches = [nid for nid, n in nodes.items() if n.node_type == "measure" and n.name == ref.name]
                    for target_id in matches:
                        edges.append(Edge(source=mid, target=target_id))
                elif kind == "column" and owner:
                    target_id = f"{owner}[{ref.name}]"
                    if target_id in nodes:
                        edges.append(Edge(source=mid, target=target_id))
                else:
                    unresolved_refs.append({"from": mid, "raw": f"[{ref.name}]"})

    # ---- usage classification ----
    usage: dict[str, UsageInfo] = {nid: UsageInfo() for nid, n in nodes.items() if n.node_type in ("measure", "column")}

    for e in edges:
        if e.target in usage:
            usage[e.target].used_by_measures.append(e.source)

    report_usage: ReportUsage = scan_report_folder(report_folder) if report_folder else ReportUsage()
    for nid, n in nodes.items():
        if n.node_type not in ("measure", "column"):
            continue
        info = usage[nid]
        if report_usage.is_used(n.table, n.name):
            info.used_in_report = True
            info.report_sources = report_usage.locations(n.table, n.name)
        elif report_folder and text_fallback_used(report_folder, n.table, n.name):
            info.used_in_report = True
            info.report_sources = ["found by text match — location not identified"]

        if info.used_in_report:
            info.level = "used-in-report"
        elif info.used_by_measures:
            info.level = "used-by-measure-only"
        else:
            info.level = "unused"

    circular = _find_cycles(nodes, edges)

    return ModelGraph(
        nodes=nodes,
        edges=edges,
        usage=usage,
        unresolved_refs=unresolved_refs,
        circular=circular,
        report_files_scanned=report_usage.scanned_files,
    )


def _find_cycles(nodes: dict[str, Node], edges: list[Edge]) -> list[list[str]]:
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        adj.setdefault(e.source, []).append(e.target)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(u: str):
        color[u] = GRAY
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v, WHITE) == WHITE:
                dfs(v)
            elif color.get(v) == GRAY:
                idx = stack.index(v)
                cycles.append(stack[idx:] + [v])
        stack.pop()
        color[u] = BLACK

    for nid in nodes:
        if color[nid] == WHITE:
            dfs(nid)

    return cycles


def dependents_of(graph: ModelGraph, node_id: str) -> list[str]:
    """Everything that (directly or transitively) references node_id — i.e.
    what breaks if node_id is deleted."""
    incoming: dict[str, list[str]] = {}
    for e in graph.edges:
        incoming.setdefault(e.target, []).append(e.source)

    seen: set[str] = set()
    stack = [node_id]
    while stack:
        cur = stack.pop()
        for src in incoming.get(cur, []):
            if src not in seen:
                seen.add(src)
                stack.append(src)
    return sorted(seen)
