# Databricks notebook source
# MAGIC %md
# MAGIC # Revenue Demo - Team Returns
# MAGIC Reads the filled-in **Team Volume Forecast** workbooks the teams return and loads them into `rev_team_returns`
# MAGIC (one tab per client account, headers on the row whose column A is `parent_account`, manual rows and Salesforce rows both kept).
# MAGIC
# MAGIC * Input: every `.xlsx` under `<schema volume>/revenue_teams/returns/`
# MAGIC   (e.g. `returns/2026/NA-North/Team Volume Forecast_NA-North_10+2.xlsx`). The file name gives the team
# MAGIC   (`NA-North`) and the cycle (`10+2`); the folder gives the year.
# MAGIC * Output: `<schema>.rev_team_returns`, one row per workbook row and month (full reload each run).
# MAGIC   Views on top: `rev_team_forecast` (latest cycle per team, by PID and platform) and
# MAGIC   `rev_team_forecast_pid` (by PID and month, what the contract database looks up).
# MAGIC * Instructions and Lists tabs are skipped; files that cannot be read are listed in `rev_team_returns_files`.

# COMMAND ----------

# MAGIC %pip install openpyxl==3.1.5 -q

# COMMAND ----------

import datetime as dt
import os
import re

from openpyxl import load_workbook
from pyspark.sql import types as T

dbutils.widgets.text("schema", "main.revenue_demo")
SCHEMA = dbutils.widgets.get("schema")
IN_ROOT = "/Volumes/" + SCHEMA.replace(".", "/") + "/revenue_teams/returns"
TARGET = f"{SCHEMA}.rev_team_returns"
FILES_TARGET = f"{SCHEMA}.rev_team_returns_files"

# Tabs that hold no forecast rows
SKIP_SHEETS = {"Instructions", "Lists"}
HEADER_KEY = "parent_account"
# Column A values that are section markers, not data
MARKERS = ("parent_account", "Parent Account", "Enter more manual rows above this line", "SALESFORCE QUERY")
MONTH_HEADER = re.compile(r"^[A-Z][a-z]{2}-\d{2}$")
INFO_COLUMNS = {
    "parent_account": "parent_account", "account_name": "account_name",
    "product_category": "product_category", "product_name": "product_name", "platform": "platform", "market": "market",
    "effective_date": "effective_date", "end_date": "end_date",
    "pricing_structure": "pricing_structure", "currency": "currency",
    "pid": "pid", "opportunity_id": "opportunity_id", "opportunity_name": "opportunity_name",
    "opportunity_stage": "opportunity_stage", "Review": "review", "Status": "status",
    "Team Leader": "team_leader", "Director": "director", "Preparer": "preparer",
}


def keep_sheet(name):
    return name not in SKIP_SHEETS


def text(v):
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    return s or None


def to_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # Excel serial left as a number
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    return None


def to_number(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    if s == "":
        return None
    if s == "-":  # accounting-format zero typed as text, e.g. " $-   "
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def product_group(category):
    # Reporting product group
    c = category or ""
    if "Amazon DSP Media" in c:
        return "DSP"
    if "Amazon Sponsored Ads" in c:
        return "AMS"
    if "Retail" in c:
        return "Retail"
    if any(x in c for x in ("Other Marketplace Sponsored Ads", "Walmart Sponsored Ads", "Other DSP Media")):
        return "OMNI"
    return None


def file_details(path):
    # File name: "Team Volume Forecast_<team>_<cycle>.xlsx", e.g. "Team Volume Forecast_NA-North_10+2.xlsx"
    name = os.path.basename(path)
    m = re.search(r"Forecast_(.+?)_(\d+)\+(\d+)", name)
    year = re.search(r"/(20\d\d)/", path)
    team = m.group(1) if m else None
    return {
        "file_name": name,
        "region": team.split("-")[0] if team and "-" in team else None,
        "team": team,
        "cycle": f"{m.group(2)}+{m.group(3)}" if m else None,
        "actual_months": int(m.group(2)) if m else None,
        "forecast_year": int(year.group(1)) if year else None,
    }


def read_workbook(path):
    """Rows of one returned workbook, one per data row and month."""
    meta = file_details(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        if not keep_sheet(ws.title):
            continue
        header = None
        section = "manual"
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            first = text(row[0]) if row else None
            if first and first.startswith("SALESFORCE QUERY"):
                section = "salesforce"
            if header is None:
                if first == HEADER_KEY:
                    header = [text(h) for h in row]
                    months = [(i, h) for i, h in enumerate(header) if h and MONTH_HEADER.match(h)]
                    notes = {h[:4]: i for i, h in enumerate(header) if h and re.match(r"^20\d\d Notes", h)}
                    info = {INFO_COLUMNS[h]: i for i, h in enumerate(header) if h in INFO_COLUMNS}
                continue
            if first is None or first.startswith(MARKERS):
                continue
            cell = lambda i: row[i] if i is not None and i < len(row) else None
            base = {k: text(cell(i)) for k, i in info.items()}
            base["effective_date"] = to_date(cell(info.get("effective_date")))
            base["end_date"] = to_date(cell(info.get("end_date")))
            for i, h in months:
                month = dt.datetime.strptime(h, "%b-%y").date()
                raw = cell(i)
                out.append({
                    **meta, **base,
                    "sheet_name": ws.title, "sheet_row": r_idx, "section": section, "product": product_group(base.get("product_category")),
                    "month": month, "amount": to_number(raw),
                    "amount_text": None if raw is None or to_number(raw) is not None else str(raw),
                    "notes": text(cell(notes.get(str(month.year)))),
                })
    wb.close()
    if meta["forecast_year"] is None and out:  # not in a year folder: the workbook's first month year
        y = min(r["month"] for r in out).year
        for r in out:
            r["forecast_year"] = y
    return out

# COMMAND ----------

paths = []
for root, _, files in os.walk(IN_ROOT):
    paths += [os.path.join(root, f) for f in files if f.lower().endswith(".xlsx") and not f.startswith("~$")]

rows, file_log = [], []
for p in sorted(paths):
    try:
        r = read_workbook(p)
        rows += r
        file_log.append((p, len({(x["sheet_name"], x["sheet_row"]) for x in r}), "loaded", None))
    except Exception as e:  # one bad file should not stop the others
        file_log.append((p, 0, "failed", f"{type(e).__name__}: {e}"[:500]))
print(f"{len(paths)} files, {len(rows)} rows")

# COMMAND ----------

schema = T.StructType([
    T.StructField("file_name", T.StringType()), T.StructField("region", T.StringType()),
    T.StructField("team", T.StringType()), T.StructField("cycle", T.StringType()),
    T.StructField("actual_months", T.IntegerType()), T.StructField("forecast_year", T.IntegerType()),
    T.StructField("sheet_name", T.StringType()), T.StructField("sheet_row", T.IntegerType()), T.StructField("section", T.StringType()),
    *[T.StructField(c, T.DateType() if c in ("effective_date", "end_date") else T.StringType()) for c in INFO_COLUMNS.values()],
    T.StructField("product", T.StringType()), T.StructField("month", T.DateType()),
    T.StructField("amount", T.DoubleType()), T.StructField("amount_text", T.StringType()), T.StructField("notes", T.StringType()),
])
df = spark.createDataFrame([{f.name: r.get(f.name) for f in schema.fields} for r in rows], schema)
(df.selectExpr("*", "current_timestamp() AS loaded_at")
   .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET))
spark.sql(f"COMMENT ON TABLE {TARGET} IS 'Team Team Volume Forecast returns, one row per workbook row and month. Loaded by notebook Revenue – Team Returns from {IN_ROOT}.'")

spark.createDataFrame(file_log, "file_path string, row_count int, status string, detail string") \
    .selectExpr("*", "current_timestamp() AS loaded_at").write.mode("overwrite").saveAsTable(FILES_TARGET)
display(spark.table(FILES_TARGET))
