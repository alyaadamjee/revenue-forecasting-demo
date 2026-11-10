# Databricks notebook source
# MAGIC %md
# MAGIC # Revenue Demo - Simulate Team Returns
# MAGIC Demo only: plays the part of the teams. Takes the workbooks the *Revenue Demo - Team Send* notebook built for a cycle
# MAGIC (`revenue_teams/sends/<cycle>/`), fills the yellow month cells with a fake forecast, marks the rows reviewed, adds one
# MAGIC manual row per workbook (a product "not in Salesforce"), and saves them to `revenue_teams/returns/<year>/<team>/` –
# MAGIC where *Revenue Demo - Team Returns* reads them.
# MAGIC
# MAGIC Fake forecast = the contract database's base amount (media spend / sales) for the PID and month, split across the PID's
# MAGIC platform rows, x a random 85–115%. Months after the contract's end that are not grey (a renewal is expected) carry the
# MAGIC average of the last three months forward. Grey months stay empty.

# COMMAND ----------

# MAGIC %pip install openpyxl==3.1.5 -q

# COMMAND ----------

import datetime as dt
import os
import random
import re
import shutil
import tempfile
from collections import defaultdict

from openpyxl import load_workbook

dbutils.widgets.text("schema", "main.revenue_demo")
dbutils.widgets.text("cycle", "10+2")
dbutils.widgets.text("seed", "7")
SCHEMA = dbutils.widgets.get("schema")
CYCLE = dbutils.widgets.get("cycle").strip()
ROOT = "/Volumes/" + SCHEMA.replace(".", "/") + "/revenue_teams"
HEADER_KEY = "parent_account"
MONTH_HEADER = re.compile(r"^[A-Z][a-z]{2}-\d{2}$")
rng = random.Random(int(dbutils.widgets.get("seed")))

base = defaultdict(dict)  # pid -> {month: base amount}
for r in spark.sql(f"SELECT contract_item_id, month, sum(base_amount) AS b FROM {SCHEMA}.rev_contract_db_monthly GROUP BY ALL").collect():
    base[r.contract_item_id][r.month] = r.b or 0.0


def is_grey(cell):
    return str(cell.fill.fgColor.rgb or "").upper().endswith("D9D9D9")


def fake_values(pid, months, share):
    """Base amount by month for one row; carries the last 3 months' average past the contract end."""
    hist = base.get(pid, {})
    out, recent = {}, []
    for m in months:
        b = hist.get(m, 0.0)
        if b:
            recent = (recent + [b])[-3:]
        elif recent:
            b = sum(recent) / len(recent)
        out[m] = round(b * share * rng.uniform(0.85, 1.15), 0)
    return out


def fill_workbook(src, dest):
    wb = load_workbook(src)
    rows_filled = 0
    for ws in wb.worksheets:
        if ws.title in ("Instructions", "Lists"):
            continue
        header_rows = [c.row for c in ws["A"] if c.value == HEADER_KEY]
        if len(header_rows) < 2:
            continue
        manual_header, sf_header = header_rows[0], header_rows[1]
        hdr = {c.value: c.column for c in ws[sf_header] if c.value}
        month_cols = [(col, dt.datetime.strptime(h, "%b-%y").date()) for h, col in hdr.items() if MONTH_HEADER.match(str(h))]
        pid_col, review_col, preparer_col = hdr["pid"], hdr["Review"], hdr["Preparer"]

        # Salesforce rows: split each PID's base across its platform rows
        sf_rows = [r for r in range(sf_header + 1, ws.max_row + 1) if ws.cell(r, 1).value]
        per_pid = defaultdict(int)
        for r in sf_rows:
            per_pid[ws.cell(r, pid_col).value] += 1
        for r in sf_rows:
            pid = ws.cell(r, pid_col).value
            vals = fake_values(pid, [m for _, m in month_cols], 1 / per_pid[pid])
            for col, m in month_cols:
                cell = ws.cell(r, col)
                cell.value = None if is_grey(cell) else vals[m]
            ws.cell(r, review_col).value = "Reviewed"
            ws.cell(r, preparer_col).value = ws.cell(r, preparer_col).value or "Demo Preparer"
            rows_filled += 1

        # One manual row on the first account tab: a product the team knows about that is not in Salesforce
        if rows_filled and not getattr(wb, "_manual_added", False) and sf_rows:
            r = manual_header + 1
            first = sf_rows[0]
            for key in ("parent_account", "account_name", "product_category", "product_name", "currency"):
                ws.cell(r, hdr[key]).value = ws.cell(first, hdr[key]).value
            ws.cell(r, hdr["platform"]).value, ws.cell(r, hdr["market"]).value = "Kroger", "US"
            ws.cell(r, hdr["pricing_structure"]).value = "% of Media (Fixed)"
            ws.cell(r, review_col).value, ws.cell(r, hdr["Status"]).value = "Reviewed", "Active"
            for col, m in month_cols:
                ws.cell(r, col).value = round(rng.uniform(5_000, 25_000), 0)
            wb._manual_added = True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = os.path.join(tempfile.mkdtemp(), os.path.basename(dest))  # write locally, then copy into the volume
    wb.save(tmp)
    shutil.copyfile(tmp, dest)
    return rows_filled

# COMMAND ----------

send_dir = f"{ROOT}/sends/{CYCLE}"
files = sorted(f for f in os.listdir(send_dir) if f.endswith(".xlsx"))
year = None
for f in files:
    team_token = re.search(r"Forecast_(.+?)_", f).group(1)
    # Forecast year = the year of the first month header; take it from the first account tab
    wb = load_workbook(f"{send_dir}/{f}", read_only=True)
    for ws in wb.worksheets[1:]:
        for row in ws.iter_rows(min_row=10, max_row=10, values_only=True):
            months = [v for v in row if v and MONTH_HEADER.match(str(v))]
            if months:
                year = 2000 + int(months[0][-2:])
        if year:
            break
    wb.close()
    dest = f"{ROOT}/returns/{year}/{team_token}/{f}"
    n = fill_workbook(f"{send_dir}/{f}", dest)
    print(f"{f}: {n} rows filled -> {dest}")
