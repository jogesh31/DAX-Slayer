"""
Scans a PBIP report folder's plain-JSON definition (definition/pages/**,
definition/report.json, bookmarks) for field usage — which measures and
columns are actually placed on a visual, used in a filter, in a
conditional-formatting rule, a tooltip, etc.

This is the piece a live XMLA connection cannot give you: TOM/DMVs know the
*model*, not the *report*. PBIP's plain-JSON layout means the report layer
is just files on disk, so we can walk it directly.

Two signals, both kept because either can miss things the other catches:

1. Structural: recursively walk each JSON file looking for the
   `{"Column": {..., "Property": X}, ...}` / `{"Measure": {..., "Property": X}}`
   shape and resolve the owning table via the nearest `SourceRef.Entity`
   (or via a `Source` alias resolved against the nearest `From` list).
2. Text fallback: a plain substring search for `Table.Column`/`[Measure]`-
   style name fragments across the raw JSON text, to catch references this
   tool's structural walk doesn't model (conditional formatting expressions,
   tooltip queryRefs, etc.). This only ever adds "used" signal, never removes
   it, so it biases toward *not* flagging something as unused rather than
   toward false confidence.

Every hit also gets resolved to a human-readable location ("Card visual on
'Overview' page") rather than the raw file path, since a GUID folder name
tells a person nothing about where to go look in Power BI Desktop.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class UsageLocation:
    page: str | None
    visual_type: str | None
    visual_title: str | None
    kind: str  # "visual" | "page-filter" | "report-filter" | "bookmark" | "other"
    # Every field (measure/column) this visual places, in the order discovered.
    # Most visuals — especially cards — never get an explicit title typed in by
    # the report author, so this is the only way to describe "what does this
    # visual actually show" without one: list what else is on it.
    all_fields: list = field(default_factory=list)
    # Where on the page, in plain words ("top-left", "center", ...). Computed
    # from the visual's on-canvas position — unlike visual_title (which is
    # only ever populated if someone explicitly typed a custom title; Power
    # BI's auto-generated titles from field names are never written to the
    # file), position data is always present, so this is the one location
    # hint that reliably helps someone actually find the visual on the page.
    region: str | None = None

    def label(self, exclude: str | None = None) -> str:
        if self.kind == "visual":
            vt = (self.visual_type or "visual").replace("Visual", "").strip() or "visual"
            base = f"{vt} visual"
            if self.visual_title:
                base += f' ("{self.visual_title}")'
            if self.page:
                base += f" on '{self.page}' page"
            if self.region:
                base += f" ({self.region})"
            others = [f for f in dict.fromkeys(self.all_fields) if f != exclude]
            if others:
                shown = ", ".join(others[:3])
                more = f" +{len(others) - 3} more" if len(others) > 3 else ""
                base += f" — also shows: {shown}{more}"
            return base
        if self.kind == "page-filter":
            return f"filter on '{self.page}' page" if self.page else "a page-level filter"
        if self.kind == "report-filter":
            return "a report-level filter"
        if self.kind == "bookmark":
            return "a bookmark"
        return "the report definition"


@dataclass
class ReportUsage:
    refs: dict[tuple[str, str], list[UsageLocation]] = field(default_factory=dict)
    scanned_files: int = 0

    def add(self, table: str | None, name: str, loc: UsageLocation):
        key = (table or "", name)
        bucket = self.refs.setdefault(key, [])
        if not any(l.label() == loc.label() for l in bucket):
            bucket.append(loc)

    def is_used(self, table: str, name: str) -> bool:
        return bool(self.refs.get((table, name)) or self.refs.get(("", name)))

    def locations(self, table: str, name: str) -> list[str]:
        locs = self.refs.get((table, name)) or self.refs.get(("", name)) or []
        return [l.label(exclude=name) for l in locs]


def _visual_title(visual_json: dict) -> str | None:
    try:
        objs = visual_json.get("visual", {}).get("objects", {})
        for t in objs.get("title", []):
            text = t.get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {}).get("Value")
            if text:
                return str(text).strip("'\"")
    except Exception:
        pass
    return None


def _visual_region(visual_json: dict, page_width: float | None, page_height: float | None) -> str | None:
    """A coarse "top-left" / "center" / "bottom-right" description of where
    on the page a visual sits, from its on-canvas position — a 3x3-grid
    read of the visual's center point against the page's dimensions.
    Unlike a title, `position` is written for every single visual, so this
    is the one location hint that's never blank."""
    try:
        pos = visual_json.get("position")
        if not pos or not page_width or not page_height:
            return None
        cx = pos["x"] + pos.get("width", 0) / 2
        cy = pos["y"] + pos.get("height", 0) / 2
        col = min(2, int(cx / page_width * 3))
        row = min(2, int(cy / page_height * 3))
        if row == 1 and col == 1:
            return "center"
        row_word = ["top", "middle", "bottom"][row]
        col_word = ["left", "center", "right"][col]
        return col_word if row == 1 else (row_word if col == 1 else f"{row_word}-{col_word}")
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def _classify_path(report_folder: str, rel: str) -> UsageLocation:
    """Turn a relative file path like
    definition/pages/<pageId>/visuals/<visualId>/visual.json
    into a human-readable location by reading the sibling page.json /
    visual.json for display names."""
    parts = rel.replace("\\", "/").split("/")

    if "visuals" in parts and rel.endswith("visual.json"):
        page_idx = parts.index("pages") + 1 if "pages" in parts else None
        page_id = parts[page_idx] if page_idx is not None else None
        page_name = None
        page_width = page_height = None
        if page_id:
            page_json_path = os.path.join(report_folder, "definition", "pages", page_id, "page.json")
            try:
                with open(page_json_path, "r", encoding="utf-8") as f:
                    page_json = json.load(f)
                page_name = page_json.get("displayName")
                page_width = page_json.get("width")
                page_height = page_json.get("height")
            except (OSError, json.JSONDecodeError):
                pass
        visual_type, visual_title, region = None, None, None
        try:
            with open(os.path.join(report_folder, rel), "r", encoding="utf-8") as f:
                vjson = json.load(f)
            visual_type = vjson.get("visual", {}).get("visualType")
            visual_title = _visual_title(vjson)
            region = _visual_region(vjson, page_width, page_height)
        except (OSError, json.JSONDecodeError):
            pass
        return UsageLocation(page=page_name, visual_type=visual_type, visual_title=visual_title, kind="visual", region=region)

    if rel.endswith("page.json") and "pages" in parts:
        page_idx = parts.index("pages") + 1
        page_id = parts[page_idx] if page_idx < len(parts) else None
        page_name = None
        if page_id:
            try:
                with open(os.path.join(report_folder, rel), "r", encoding="utf-8") as f:
                    page_name = json.load(f).get("displayName")
            except (OSError, json.JSONDecodeError):
                pass
        return UsageLocation(page=page_name, visual_type=None, visual_title=None, kind="page-filter")

    if "bookmarks" in parts:
        return UsageLocation(page=None, visual_type=None, visual_title=None, kind="bookmark")

    if rel.endswith("report.json"):
        return UsageLocation(page=None, visual_type=None, visual_title=None, kind="report-filter")

    return UsageLocation(page=None, visual_type=None, visual_title=None, kind="other")


def _walk(node, aliases: dict[str, str], usage: ReportUsage, report_folder: str, source: str, loc_cache: dict):
    if isinstance(node, dict):
        if "From" in node and isinstance(node["From"], list):
            for f in node["From"]:
                if isinstance(f, dict) and "Name" in f and "Entity" in f:
                    aliases = {**aliases, f["Name"]: f["Entity"]}

        for key in ("Column", "Measure", "HierarchyLevel"):
            if key in node and isinstance(node[key], dict):
                sub = node[key]
                prop = sub.get("Property")
                if prop:
                    entity = None
                    expr = sub.get("Expression", {})
                    src_ref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                    if "Entity" in src_ref:
                        entity = src_ref["Entity"]
                    elif "Source" in src_ref:
                        entity = aliases.get(src_ref["Source"])
                    if source not in loc_cache:
                        loc_cache[source] = _classify_path(report_folder, source)
                    loc = loc_cache[source]
                    if prop not in loc.all_fields:
                        loc.all_fields.append(prop)
                    usage.add(entity, prop, loc)

        for v in node.values():
            _walk(v, aliases, usage, report_folder, source, loc_cache)
    elif isinstance(node, list):
        for item in node:
            _walk(item, aliases, usage, report_folder, source, loc_cache)


def scan_report_folder(report_folder: str) -> ReportUsage:
    usage = ReportUsage()
    if not report_folder or not os.path.isdir(report_folder):
        return usage

    loc_cache: dict = {}
    for root, _dirs, files in os.walk(report_folder):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, report_folder)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            usage.scanned_files += 1
            _walk(data, {}, usage, report_folder, rel, loc_cache)

    return usage


def text_fallback_used(report_folder: str, table: str, name: str) -> bool:
    """Cheap secondary net: does `Table.Name` or `[Name]` appear literally
    anywhere in the report JSON text? Only ever adds usage signal."""
    if not report_folder or not os.path.isdir(report_folder):
        return False
    needles = [f"{table}.{name}", f"[{name}]"]
    for root, _dirs, files in os.walk(report_folder):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, fname), "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            if any(needle in text for needle in needles):
                return True
    return False
