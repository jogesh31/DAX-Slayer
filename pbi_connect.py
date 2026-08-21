"""
Connection layer to a locally-running Power BI Desktop report.

Read path uses ADOMD.NET (same approach as the sibling pbi_validator /
pbi_model_explainer tools in this folder): borrows the ADOMD.NET / AMO
client assemblies that ship inside a local DAX Studio install, no separate
download needed.

Write path (for deleting unused objects) additionally loads the Tabular
Object Model (Microsoft.AnalysisServices.Tabular) from the same DAX Studio
bin folder, and connects a TOM `Server`/`Database` to the same local
instance so deletes go through the real model API (with SaveChanges),
not raw XMLA text.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import pandas as pd
import psutil

DAX_STUDIO_BIN_CANDIDATES = [
    r"C:\Program Files\DAX Studio\bin",
    r"C:\Program Files (x86)\DAX Studio\bin",
]

REQUIRED_DLLS = [
    "Microsoft.AnalysisServices.AdomdClient.dll",
    "Microsoft.AnalysisServices.Core.dll",
    "Microsoft.AnalysisServices.dll",
    "Microsoft.AnalysisServices.Tabular.dll",
]


@dataclass
class LocalInstance:
    port: int
    process_id: int
    report_title: str | None = None
    file_path: str | None = None       # the .pbix/.pbip Power BI Desktop actually has open
    report_folder: str | None = None   # sibling *.Report folder, only exists for a .pbip project
    is_pbix: bool = False              # opened from .pbix — no report-layout folder can ever exist


def find_dax_studio_bin() -> str:
    env_override = os.environ.get("DAX_STUDIO_BIN")
    candidates = ([env_override] if env_override else []) + DAX_STUDIO_BIN_CANDIDATES
    for candidate in candidates:
        if candidate and all(os.path.exists(os.path.join(candidate, dll)) for dll in REQUIRED_DLLS):
            return candidate
    raise FileNotFoundError(
        "Could not find ADOMD.NET / AMO client assemblies. Install DAX Studio "
        "(free, from daxstudio.org) or set DAX_STUDIO_BIN to a folder "
        "containing the Microsoft.AnalysisServices*.dll files."
    )


def _window_title_for_pid(pid: int) -> str | None:
    try:
        import ctypes

        titles: list[str] = []

        def callback(hwnd, _lparam):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            found_pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
            if found_pid.value == pid:
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    if buf.value:
                        titles.append(buf.value)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(callback), 0)
        return titles[0] if titles else None
    except Exception:
        return None


def _opened_file_for_pid(pid: int) -> str | None:
    """Power BI Desktop is launched as `PBIDesktop.exe "<path to the file
    that was opened>"` — reading that command line is a far more reliable
    way to find the report's project folder than fuzzy-matching window
    titles against nearby folder names, and it works with zero user input,
    the same way DAX Studio auto-detects what's open."""
    try:
        proc = psutil.Process(pid)
        for arg in proc.cmdline():
            lower = arg.lower()
            if lower.endswith(".pbix") or lower.endswith(".pbip"):
                return arg
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return None


def _report_folder_for_file(file_path: str) -> str | None:
    """A .pbip project stores its report layer in a sibling `<name>.Report`
    folder next to the .pbip file itself — no searching needed once we know
    the actual project path."""
    if not file_path.lower().endswith(".pbip"):
        return None
    base = os.path.splitext(file_path)[0]
    candidate = base + ".Report"
    return candidate if os.path.isdir(candidate) else None


def discover_local_instances() -> list[LocalInstance]:
    instances: list[LocalInstance] = []

    msmdsrv_pids: dict[int, int | None] = {}
    for proc in psutil.process_iter(["pid", "name", "ppid"]):
        try:
            if (proc.info["name"] or "").lower() == "msmdsrv.exe":
                msmdsrv_pids[proc.info["pid"]] = proc.info["ppid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not msmdsrv_pids:
        return instances

    seen: set[tuple[int, int]] = set()
    for conn in psutil.net_connections(kind="inet"):
        if conn.pid in msmdsrv_pids and conn.status == psutil.CONN_LISTEN and conn.laddr:
            key = (conn.pid, conn.laddr.port)
            if key in seen:
                continue
            seen.add(key)
            parent_pid = msmdsrv_pids[conn.pid]
            title = _window_title_for_pid(parent_pid) if parent_pid else None
            file_path = _opened_file_for_pid(parent_pid) if parent_pid else None
            report_folder = _report_folder_for_file(file_path) if file_path else None
            is_pbix = bool(file_path and file_path.lower().endswith(".pbix"))
            instances.append(
                LocalInstance(
                    port=conn.laddr.port, process_id=conn.pid, report_title=title,
                    file_path=file_path, report_folder=report_folder, is_pbix=is_pbix,
                )
            )
    return instances


_clr_loaded = False


def _ensure_clr_loaded():
    global _clr_loaded
    if _clr_loaded:
        return

    import clr  # noqa: F401  (pythonnet)

    bin_dir = find_dax_studio_bin()
    import sys

    sys.path.append(bin_dir)
    for dll in REQUIRED_DLLS:
        clr.AddReference(os.path.join(bin_dir, dll))

    _clr_loaded = True


class PowerBIConnection:
    """A connection to one open Power BI Desktop report's local model.

    Exposes both the read-only ADOMD.NET query path and a TOM `Database`
    handle for schema mutation (deleting measures/columns).
    """

    def __init__(self, port: int):
        _ensure_clr_loaded()
        from Microsoft.AnalysisServices.AdomdClient import AdomdConnection

        self.port = port
        conn_str = f"Data Source=localhost:{port}"
        self._conn = AdomdConnection(conn_str)
        self._conn.Open()
        self._tom_server = None
        self._tom_database = None

    def close(self):
        if self._tom_server is not None:
            try:
                self._tom_server.Disconnect()
            except Exception:
                pass
        self._conn.Close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- read path (ADOMD.NET DMV queries) ------------------------------

    def query(self, dax: str) -> pd.DataFrame:
        from Microsoft.AnalysisServices.AdomdClient import AdomdCommand

        cmd = AdomdCommand(dax, self._conn)
        reader = cmd.ExecuteReader()
        columns = [reader.GetName(i) for i in range(reader.FieldCount)]
        rows = []
        while reader.Read():
            rows.append([reader.GetValue(i) for i in range(reader.FieldCount)])
        reader.Close()
        df = pd.DataFrame(rows, columns=columns)
        df.columns = [re.sub(r"^\[|\]$", "", c).split("[")[-1].rstrip("]") for c in df.columns]
        return df

    def list_tables(self) -> pd.DataFrame:
        return self.query("EVALUATE INFO.VIEW.TABLES()")

    def list_columns(self) -> pd.DataFrame:
        return self.query("EVALUATE INFO.VIEW.COLUMNS()")

    def list_measures(self) -> pd.DataFrame:
        return self.query("EVALUATE INFO.VIEW.MEASURES()")

    def list_relationships(self) -> pd.DataFrame:
        return self.query("EVALUATE INFO.VIEW.RELATIONSHIPS()")

    # ---- write path (TOM) ------------------------------------------------

    def tom_database(self):
        """Lazily connect a TOM Server/Database to the same local instance."""
        if self._tom_database is not None:
            return self._tom_database

        _ensure_clr_loaded()
        from Microsoft.AnalysisServices.Tabular import Server

        server = Server()
        server.Connect(f"Data Source=localhost:{self.port}")
        if server.Databases.Count == 0:
            raise RuntimeError("No database found on this local Power BI Desktop instance.")
        database = server.Databases[0]
        self._tom_server = server
        self._tom_database = database
        return database

    def export_bim_snapshot(self, path: str) -> None:
        """Serialize the full current model (TMSL/BIM JSON) to disk before
        making any destructive change, so a delete can always be reversed
        by hand even outside this tool."""
        from Microsoft.AnalysisServices.Tabular import JsonSerializer

        db = self.tom_database()
        json_text = JsonSerializer.SerializeDatabase(db)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(json_text))

    def delete_objects(self, deletions: list[dict]) -> None:
        """deletions: list of {"type": "measure"|"column", "table": str, "name": str}.
        Applies all deletions, then SaveChanges() once as a single transaction —
        if SaveChanges throws, TOM has made no changes to the live model."""
        db = self.tom_database()
        model = db.Model

        for d in deletions:
            table = model.Tables.Find(d["table"])
            if table is None:
                raise RuntimeError(f"Table not found: {d['table']}")
            if d["type"] == "measure":
                obj = table.Measures.Find(d["name"])
                if obj is None:
                    raise RuntimeError(f"Measure not found: {d['table']}[{d['name']}]")
                table.Measures.Remove(obj)
            elif d["type"] == "column":
                obj = table.Columns.Find(d["name"])
                if obj is None:
                    raise RuntimeError(f"Column not found: {d['table']}[{d['name']}]")
                table.Columns.Remove(obj)
            else:
                raise ValueError(f"Unknown deletion type: {d['type']}")

        db.Model.SaveChanges()

    def _find_expression_object(self, model, kind: str, table: str, name: str):
        t = model.Tables.Find(table)
        if t is None:
            raise RuntimeError(f"Table not found: {table}")
        obj = t.Measures.Find(name) if kind == "measure" else t.Columns.Find(name)
        if obj is None:
            noun = "Measure" if kind == "measure" else "Column"
            raise RuntimeError(f"{noun} not found: {table}[{name}]")
        return obj

    def set_measure_expression(self, table: str, name: str, new_expression: str) -> None:
        """Overwrite a measure's DAX with reformatted (or otherwise edited)
        text — same shape as the DisplayFolder write: find the object, set
        the property, SaveChanges once. Callers are expected to have taken
        a BIM backup first, same as every other write path in this file."""
        db = self.tom_database()
        obj = self._find_expression_object(db.Model, "measure", table, name)
        obj.Expression = new_expression
        db.Model.SaveChanges()

    def set_column_expression(self, table: str, name: str, new_expression: str) -> None:
        """Same as set_measure_expression, for a calculated column."""
        db = self.tom_database()
        obj = self._find_expression_object(db.Model, "column", table, name)
        obj.Expression = new_expression
        db.Model.SaveChanges()

    def set_expressions_bulk(self, updates: list[dict]) -> None:
        """Bulk version of set_measure_expression/set_column_expression —
        updates: list of {"kind": "measure"|"column", "table", "name",
        "expression"}. Applies every change, then SaveChanges() once as a
        single transaction (same all-or-nothing shape as delete_objects),
        so "Format All" either lands cleanly or leaves the live model
        completely untouched."""
        db = self.tom_database()
        model = db.Model
        for u in updates:
            obj = self._find_expression_object(model, u["kind"], u["table"], u["name"])
            obj.Expression = u["expression"]
        db.Model.SaveChanges()

    def create_measure(self, table: str, name: str, expression: str) -> None:
        """Adds a brand-new measure — used by the "Most Used DAX" library's
        deploy action, where the DAX text is a generic template the user has
        just edited to match their own table/column names. Refuses to
        silently overwrite an existing measure of the same name; the caller
        is expected to have taken a BIM backup first."""
        from Microsoft.AnalysisServices.Tabular import Measure

        db = self.tom_database()
        model = db.Model
        t = model.Tables.Find(table)
        if t is None:
            raise RuntimeError(f"Table not found: {table}")
        if t.Measures.Find(name) is not None:
            raise RuntimeError(f"A measure named '{name}' already exists on {table}. Pick a different name.")
        m = Measure()
        m.Name = name
        m.Expression = expression
        t.Measures.Add(m)
        db.Model.SaveChanges()

    def create_column(self, table: str, name: str, expression: str) -> None:
        """Adds a brand-new calculated column — same deploy path as
        create_measure, for the handful of snippets (e.g. Fiscal Year) that
        are column expressions rather than measures. DataType is left
        Automatic; Power BI infers it on the next model refresh, same as
        when you type a calculated column directly in Desktop."""
        from Microsoft.AnalysisServices.Tabular import CalculatedColumn, DataType

        db = self.tom_database()
        model = db.Model
        t = model.Tables.Find(table)
        if t is None:
            raise RuntimeError(f"Table not found: {table}")
        if t.Columns.Find(name) is not None:
            raise RuntimeError(f"A column named '{name}' already exists on {table}. Pick a different name.")
        c = CalculatedColumn()
        c.Name = name
        c.Expression = expression
        c.DataType = DataType.Automatic
        t.Columns.Add(c)
        db.Model.SaveChanges()

    def create_table(self, name: str, expression: str) -> None:
        """Adds a brand-new calculated table (e.g. a Date table snippet) —
        the third deploy path alongside create_measure/create_column.

        Unlike a measure or column, a calculated table has no columns to
        set up front: TOM only discovers its schema by actually evaluating
        the DAX against the engine. So this is a two-step SaveChanges — the
        first creates the table+partition shell, the second (after
        RequestRefresh) is what makes the engine run the expression and
        populate real columns. This is the same thing Power BI Desktop's UI
        does the moment you finish typing a calculated table's DAX."""
        from Microsoft.AnalysisServices.Tabular import CalculatedPartitionSource, Partition, RefreshType, Table

        db = self.tom_database()
        model = db.Model
        if model.Tables.Find(name) is not None:
            raise RuntimeError(f"A table named '{name}' already exists. Pick a different name.")

        table = Table()
        table.Name = name
        partition = Partition()
        partition.Name = name
        source = CalculatedPartitionSource()
        source.Expression = expression
        partition.Source = source
        table.Partitions.Add(partition)
        model.Tables.Add(table)
        db.Model.SaveChanges()

        table.RequestRefresh(RefreshType.Full)
        db.Model.SaveChanges()

    def set_measures_folder(self, folder_name: str, measures: list[dict] | None = None) -> int:
        """Set DisplayFolder on measures so they group into one folder in
        Power BI's field list. measures=None means every measure in the
        model, regardless of table; otherwise a list of {"table","name"}
        restricts it to a subset. Returns how many measures were changed.

        DisplayFolder supports "Parent\\Child" nesting, but this always sets
        a single flat folder — that's the "bring them all into one folder"
        request, not a full re-org tool."""
        db = self.tom_database()
        model = db.Model

        targets = []
        if measures is None:
            for table in model.Tables:
                for m in table.Measures:
                    targets.append(m)
        else:
            for d in measures:
                table = model.Tables.Find(d["table"])
                if table is None:
                    raise RuntimeError(f"Table not found: {d['table']}")
                m = table.Measures.Find(d["name"])
                if m is None:
                    raise RuntimeError(f"Measure not found: {d['table']}[{d['name']}]")
                targets.append(m)

        for m in targets:
            m.DisplayFolder = folder_name

        db.Model.SaveChanges()
        return len(targets)


def connect_first_available() -> PowerBIConnection:
    instances = discover_local_instances()
    if not instances:
        raise RuntimeError(
            "No open Power BI Desktop reports found. Open the .pbix/.pbip and try again."
        )
    return PowerBIConnection(instances[0].port)
