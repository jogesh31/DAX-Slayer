# DAX Slayer — Setup

A "Measure Killer"/DAX Studio-style tool: connects to a Power BI Desktop
report that's currently open, maps every measure/column dependency, flags
objects that are genuinely unused (checked against both the model's own
DAX and, if you point it at the report's PBIP folder, the actual report
pages/visuals), and lets you stage deletions with a dependency-impact
preview before deploying them for real.

## Requirements

- **Python 3.10+** (https://python.org — tick "Add python.exe to PATH").
- **DAX Studio** (free, https://daxstudio.org) — not used directly, but its
  install folder supplies the ADOMD.NET / Tabular Object Model client DLLs
  this tool borrows to talk to Power BI Desktop. If it's installed
  somewhere other than `C:\Program Files\DAX Studio\bin`, set the
  `DAX_STUDIO_BIN` environment variable to the right folder.
- Power BI Desktop, with the report you want to clean up open.

## Running standalone

Double-click **`Run Dependency Analyzer.bat`**. First run creates an
isolated `.venv` and installs dependencies (a minute or two); every run
after that is instant. It opens `http://127.0.0.1:8765` in your browser.

1. Pick the open report from the **instance dropdown** (top bar) and click
   **Connect**.
2. (Recommended) Pick the matching **`.Report` folder** from the second
   dropdown — this is what lets the tool check whether a measure/column is
   actually placed on a report page, not just referenced by other DAX. Only
   works for PBIP-format projects (File > Save As > Power BI Project);
   skip it for a plain `.pbix` and usage detection falls back to DAX
   cross-references only.
3. Click **Analyze Model**. The graph, object browser, and unused-object
   list populate.
4. Click any node (or list item) to see its DAX, what depends on it, and a
   **Stage for Deletion** button. Staging never touches Power BI — it's a
   local pending list you can add to / remove from freely.
5. When ready, **Deploy Deletions to Power BI** in the right panel. This
   writes a full model snapshot to `backups/*.bim` first, then applies all
   staged deletions as a single transaction via the Tabular Object Model
   and saves. If it fails partway, nothing is written to the live model
   (TOM only commits on a clean `SaveChanges`).

## Registering as a Power BI External Tool (optional)

This makes "DAX Slayer" show up directly in Power BI Desktop's
**External Tools** ribbon, auto-connected to whichever report you launched
it from.

1. Double-click **`Install External Tool.bat`** and approve the admin
   permission prompt — it copies `pbitool.json` into Power BI's External
   Tools folder for you (writing there needs admin rights, which is why it
   asks). Or copy it there by hand:
   `C:\Program Files (x86)\Common Files\Microsoft Shared\Power BI Desktop\External Tools\`
2. If this project folder is at a different path than where you installed
   from, edit the `arguments` field in `pbitool.json` so the embedded path
   points at your actual `launch_external_tool.bat` location, then reinstall.
3. Restart Power BI Desktop. The tool now appears under **External Tools**
   in the ribbon whenever a report is open.

## Notes on unused-object detection

Two independent signals feed the usage classification:

- **DAX cross-references** (always on): does any other measure's DAX
  expression reference this measure/column?
- **Report usage** (only if you selected a `.Report` folder): does any
  visual, filter, or bookmark reference it? PBIP stores the report layer
  as plain JSON, so this is read directly off disk rather than guessed.

An object is "unused" only if **neither** signal finds a reference. This
deliberately errs toward under-flagging rather than over-flagging — a
measure with an unusual reference pattern the scanner doesn't recognize
will show as "used" (safe) rather than "unused" (risky), so always check
the dependency panel before deploying a deletion regardless of the badge
color.

## Limitations (v1)

- No in-app undo after **Deploy** — recovery is via the `.bim` snapshot in
  `backups/`, importable back into the model by hand (Tabular Editor or
  re-running TOM deserialize) if you need to reverse a deploy.
- Works against one open report at a time.
- DirectQuery-only measures/columns are supported for dependency analysis
  (it's metadata, not data) exactly the same as Import mode.
