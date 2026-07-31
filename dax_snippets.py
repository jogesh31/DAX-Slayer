"""
"Most Used DAX" — a curated library of the DAX patterns that show up in
almost every Power BI dashboard: a Date table, time-intelligence measures
(YTD/QTD/MTD, YoY growth), ranking, running totals, and percent-of-total.

Static data, same spirit as dax_functions.py: ships offline, stable,
auditable — not fetched or generated at runtime. Meant to be browsed and
copy-pasted into a model, not tied to whatever report is currently
connected (unlike the rest of this tool).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SnippetInfo:
    name: str
    category: str
    description: str
    dax: str  # full "ObjectName =\n<expression>" text, for the Copy button
    kind: str = "measure"  # "measure" | "column" | "table" — what deploying this actually creates
    default_object_name: str = ""  # parsed from `dax`'s left side — the name a real measure/column would get
    expression: str = ""  # parsed from `dax`'s right side — what the deploy editor shows (no name prefix)


# Snippets whose `kind` isn't the default "measure". The two full Date
# tables need a calculated-TABLE deploy path (a different TOM object shape
# than measures/columns — Copy-only for now); Fiscal Year is a calculated
# COLUMN on an existing Date table.
_KIND_OVERRIDES = {
    "Date Table (Calendar)": "table",
    "Date Table (Auto range from Fact table)": "table",
    "Fiscal Year": "column",
}

_RAW = [
    # ---- Date Table -------------------------------------------------------
    (
        "Date Table (Calendar)",
        "Date Table",
        "The single most common building block in any model — a proper Date table with one row per day, marked as a Date Table so time intelligence functions (TOTALYTD, SAMEPERIODLASTYEAR, etc.) work correctly.",
        """Date =
ADDCOLUMNS(
    CALENDAR(DATE(2020,1,1), DATE(2030,12,31)),
    "Year", YEAR([Date]),
    "Month Number", MONTH([Date]),
    "Month Name", FORMAT([Date], "MMMM"),
    "Month Short", FORMAT([Date], "MMM"),
    "Quarter", "Q" & FORMAT([Date], "Q"),
    "Year Quarter", YEAR([Date]) & "-Q" & FORMAT([Date], "Q"),
    "Year Month", FORMAT([Date], "YYYY-MM"),
    "Day Of Week", FORMAT([Date], "dddd"),
    "Is Weekend", WEEKDAY([Date], 2) > 5
)""",
    ),
    (
        "Date Table (Auto range from Fact table)",
        "Date Table",
        "Same idea, but the range is derived automatically from whatever dates already exist in your fact table instead of a hardcoded start/end — one less thing to maintain.",
        """Date =
ADDCOLUMNS(
    CALENDAR(MIN(Sales[OrderDate]), MAX(Sales[OrderDate])),
    "Year", YEAR([Date]),
    "Month Name", FORMAT([Date], "MMMM"),
    "Quarter", "Q" & FORMAT([Date], "Q")
)""",
    ),
    (
        "Fiscal Year",
        "Date Table",
        "A calculated column for organizations whose fiscal year doesn't start in January — shift the year number forward once the calendar crosses the fiscal start month.",
        """Fiscal Year = 'Date'[Year] + IF(MONTH('Date'[Date]) >= 4, 1, 0)""",
    ),

    # ---- Time Intelligence --------------------------------------------------
    (
        "Total Sales YTD",
        "Time Intelligence",
        "Running year-to-date total — the classic time-intelligence measure, requires a marked Date table.",
        """Total Sales YTD =
TOTALYTD(SUM(Sales[Amount]), 'Date'[Date])""",
    ),
    (
        "Total Sales QTD",
        "Time Intelligence",
        "Running quarter-to-date total.",
        """Total Sales QTD =
TOTALQTD(SUM(Sales[Amount]), 'Date'[Date])""",
    ),
    (
        "Total Sales MTD",
        "Time Intelligence",
        "Running month-to-date total.",
        """Total Sales MTD =
TOTALMTD(SUM(Sales[Amount]), 'Date'[Date])""",
    ),
    (
        "Sales Same Period Last Year",
        "Time Intelligence",
        "Shifts the current filter context back exactly one year — the base every YoY comparison is built on.",
        """Sales PY =
CALCULATE(
    SUM(Sales[Amount]),
    SAMEPERIODLASTYEAR('Date'[Date])
)""",
    ),
    (
        "YoY Growth %",
        "Time Intelligence",
        "Percent change versus the same period last year — pairs with the snippet above. DIVIDE handles the zero-denominator case safely.",
        """YoY Growth % =
VAR CurrentSales = SUM(Sales[Amount])
VAR PriorYearSales =
    CALCULATE(SUM(Sales[Amount]), SAMEPERIODLASTYEAR('Date'[Date]))
RETURN
DIVIDE(CurrentSales - PriorYearSales, PriorYearSales)""",
    ),
    (
        "MoM Growth %",
        "Time Intelligence",
        "Percent change versus the previous month.",
        """MoM Growth % =
VAR CurrentSales = SUM(Sales[Amount])
VAR PriorMonthSales =
    CALCULATE(SUM(Sales[Amount]), PREVIOUSMONTH('Date'[Date]))
RETURN
DIVIDE(CurrentSales - PriorMonthSales, PriorMonthSales)""",
    ),
    (
        "Rolling 12-Month Total",
        "Time Intelligence",
        "A trailing 12-month total that moves with whatever month is selected — common for trend charts that shouldn't reset every January.",
        """Rolling 12M Sales =
CALCULATE(
    SUM(Sales[Amount]),
    DATESINPERIOD('Date'[Date], MAX('Date'[Date]), -12, MONTH)
)""",
    ),

    # ---- Running Totals & Rankings -----------------------------------------
    (
        "Running Total",
        "Running Totals & Ranking",
        "A cumulative total across whatever's on the axis (e.g. date) — filters everything up to and including the current row using ALLSELECTED + max-date comparison.",
        """Running Total Sales =
CALCULATE(
    SUM(Sales[Amount]),
    FILTER(
        ALLSELECTED('Date'[Date]),
        'Date'[Date] <= MAX('Date'[Date])
    )
)""",
    ),
    (
        "Rank by Sales",
        "Running Totals & Ranking",
        "Ranks the current item (e.g. product, salesperson) against every other item by a measure — RANKX is the standard way to build a leaderboard.",
        """Sales Rank =
RANKX(
    ALL(Product[ProductName]),
    [Total Sales],
    ,
    DESC
)""",
    ),
    (
        "Top N Filter Measure",
        "Running Totals & Ranking",
        "A measure used inside a visual-level filter (set to \"is not blank\") to keep only the top N items by sales — a common alternative to the built-in Top N filter when you need it to combine with other logic.",
        """Is Top 10 by Sales =
IF(
    RANKX(ALL(Product[ProductName]), [Total Sales]) <= 10,
    [Total Sales]
)""",
    ),

    # ---- Percentages & Ratios -----------------------------------------------
    (
        "% of Grand Total",
        "Percentages & Ratios",
        "Each row's share of the overall total, ignoring whatever filters are applied to the category on the axis — the standard \"% of total\" measure.",
        """% of Grand Total =
DIVIDE(
    SUM(Sales[Amount]),
    CALCULATE(SUM(Sales[Amount]), ALL(Sales))
)""",
    ),
    (
        "% of Parent Category",
        "Percentages & Ratios",
        "Share of the immediately containing group (e.g. a product's share of its category) rather than the whole table — swap ALL(Sales) for ALLEXCEPT to only remove the lower-level filter.",
        """% of Category =
DIVIDE(
    SUM(Sales[Amount]),
    CALCULATE(SUM(Sales[Amount]), ALLEXCEPT(Sales, Sales[Category]))
)""",
    ),
    (
        "Safe Division",
        "Percentages & Ratios",
        "The single most common defensive-DAX pattern — DIVIDE returns blank (or a chosen fallback) instead of erroring when the denominator is zero.",
        """Conversion Rate =
DIVIDE([Total Orders], [Total Visitors], 0)""",
    ),

    # ---- Core Aggregations --------------------------------------------------
    (
        "Total Sales",
        "Core Aggregations",
        "The most basic measure in any model — everyone writes this one first.",
        """Total Sales = SUM(Sales[Amount])""",
    ),
    (
        "Distinct Customer Count",
        "Core Aggregations",
        "Counts unique customers rather than order rows — the usual fix when a naive COUNT overstates a metric like \"customers\".",
        """Distinct Customers = DISTINCTCOUNT(Sales[CustomerID])""",
    ),
    (
        "Average Order Value",
        "Core Aggregations",
        "Total revenue divided by number of distinct orders — a very common derived KPI, built safely with DIVIDE.",
        """Average Order Value =
DIVIDE(
    SUM(Sales[Amount]),
    DISTINCTCOUNT(Sales[OrderID])
)""",
    ),
    (
        "Dynamic Title with Selected Filter",
        "Core Aggregations",
        "Builds a chart/page title that reflects the current slicer selection, falling back to a generic label when multiple values are selected — extremely common on report headers.",
        """Selected Category Title =
"Sales — " & COALESCE(SELECTEDVALUE(Product[Category]), "All Categories")""",
    ),
]

def _build(name: str, category: str, description: str, dax: str) -> SnippetInfo:
    # Every snippet is authored as "ObjectName =\n<expression>" — split on
    # the first "=" to get the name TOM would actually create, and the
    # expression on its own (what the deploy editor shows, no name prefix).
    eq_idx = dax.index("=")
    default_object_name = dax[:eq_idx].strip()
    expression = dax[eq_idx + 1:].strip()
    kind = _KIND_OVERRIDES.get(name, "measure")
    return SnippetInfo(name, category, description, dax, kind, default_object_name, expression)


SNIPPETS: list[SnippetInfo] = [_build(*row) for row in _RAW]


def all_snippets() -> list[SnippetInfo]:
    return SNIPPETS
