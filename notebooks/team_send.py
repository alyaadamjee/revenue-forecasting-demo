# Databricks notebook source
# MAGIC %md
# MAGIC # Revenue Demo - Team Send
# MAGIC Builds each team's **Team Volume Forecast** workbook for every send scheduled in the **Revenue Team Send** app
# MAGIC (`rev_send_schedule`, status `scheduled`, send time passed) and, once email is set up, sends it.
# MAGIC
# MAGIC * Workbook: one tab per parent account with a Manual Entries table and a Salesforce Query table (header rows 9–10),
# MAGIC   Salesforce columns A:N, Team Forecast Input columns O:AQ (Review, Status, Team Leader, Director, Preparer, Jan–Dec of the
# MAGIC   forecast year and the next with FY totals and notes). The notebook *Revenue Demo - Team Returns* reads the returns back by
# MAGIC   these headers and markers, so keep them stable.
# MAGIC * Salesforce rows: `rev_team_workbook_rows` (Salesforce, PID × platform × market) ending on or after Jan 1 of the forecast year.
# MAGIC * Prefill: the team's latest return in `rev_team_returns` – months, Status, Director, Preparer and
# MAGIC   notes by PID × platform × market; manual rows carried over. Review resets to Not Reviewed; rows new since then get Status New.
# MAGIC   Months outside the contract's dates (and after the end date when there is no next opportunity) are grey – no forecast needed.
# MAGIC * Output: `<schema volume>/revenue_teams/sends/<cycle>/Team Volume Forecast_<team>_<cycle>.xlsx`,
# MAGIC   one log row per team in `rev_send_log`.
# MAGIC * Email: sent through Microsoft Graph when secret scope `rev_team_send` holds `tenant_id`, `client_id`, `client_secret` and `sender`
# MAGIC   (an Entra ID app registration with Mail.Send). Without it the log says `email not configured` and the files wait in the volume.
# MAGIC
# MAGIC * Gate: runs the notebook *Revenue Demo - Salesforce Data Checks* first; if any check errors, every due send is marked failed
# MAGIC   with the reasons in `rev_send_log` and no workbook is built.
# MAGIC
# MAGIC Widget `send_id`: run one send now whatever its time or status (used by the app's Send now button).

# COMMAND ----------

# MAGIC %pip install openpyxl==3.1.5 -q

# COMMAND ----------

import base64
import datetime as dt
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

dbutils.widgets.text("schema", "main.revenue_demo")
SCHEMA = dbutils.widgets.get("schema")
RETURNS = f"{SCHEMA}.rev_team_returns"
OUT_ROOT = "/Volumes/" + SCHEMA.replace(".", "/") + "/revenue_teams/sends"
SECRET_SCOPE = "rev_team_send"
CHECKS_NOTEBOOK = "./Revenue Demo - Salesforce Data Checks"  # must pass before any workbook is built
CHECK_RUNS, CHECK_LOG = f"{SCHEMA}.rev_sf_check_runs", f"{SCHEMA}.rev_sf_check_log"

dbutils.widgets.text("send_id", "")
forced_send_id = dbutils.widgets.get("send_id").strip()

# COMMAND ----------

def log(send_id, team, file_path, line_count, status, detail=None):
    spark.sql(
        f"INSERT INTO {SCHEMA}.rev_send_log VALUES (:send_id, :team, :file_path, :line_count, :status, :detail, current_timestamp())",
        args={"send_id": send_id, "team": team, "file_path": file_path, "line_count": line_count, "status": status, "detail": detail},
    )


def set_status(send_id, status):
    spark.sql(
        f"UPDATE {SCHEMA}.rev_send_schedule SET status = :status, updated_at = current_timestamp() WHERE send_id = :send_id",
        args={"status": status, "send_id": send_id},
    )


def graph_settings():
    try:
        keys = {k.key for k in dbutils.secrets.list(SECRET_SCOPE)}
    except Exception:
        return None
    need = {"tenant_id", "client_id", "client_secret", "sender"}
    if not need <= keys:
        return None
    return {k: dbutils.secrets.get(SECRET_SCOPE, k) for k in need}


def send_mail(cfg, to, cc, subject, body, attachment_path):
    token = requests.post(
        f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
              "scope": "https://graph.microsoft.com/.default"},
        timeout=30,
    ).json()["access_token"]
    with open(attachment_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": os.path.basename(attachment_path),
                         "contentBytes": content}],
    }
    r = requests.post(f"https://graph.microsoft.com/v1.0/users/{cfg['sender']}/sendMail",
                      headers={"Authorization": f"Bearer {token}"}, json={"message": message}, timeout=60)
    r.raise_for_status()


def split_emails(s):
    return [a.strip() for a in re.split(r"[,;]", s or "") if a.strip()]

# COMMAND ----------

# Workbook layout: columns A:N Salesforce fields, O:S team fields, T:AE year 1 months, AF FY, AG notes, AH spacer,
# AI:AT year 2 months, AU FY, AV notes.
INFO = [  # (display header, technical header, row key)
    ("Parent Account", "parent_account", "parent_account"), ("Account Name", "account_name", "account_name"),
    ("Product Category", "product_category", "product_category"), ("Product Name", "product_name", "product_name"),
    ("Platform", "platform", "platform"), ("Market", "market", "market"),
    ("Effective Date", "effective_date", "effective_date"), ("End Date", "end_date", "end_date"),
    ("Pricing Structure", "pricing_structure", "pricing_structure"), ("Currency", "currency", "currency"),
    ("Product ID", "pid", "pid"), ("Opportunity ID", "opportunity_id", "opportunity_id"),
    ("Opportunity Name", "opportunity_name", "opportunity_name"), ("Opportunity Stage", "opportunity_stage", "opportunity_stage"),
]
TEAM_INPUT = [  # team input columns
    ("Review", "review"), ("Status", "status"), ("Team Leader", "team_leader"), ("Director", "director"), ("Preparer", "preparer")]
C_TEAM = len(INFO) + 1                      # O
C_Y1 = C_TEAM + len(TEAM_INPUT)                       # T
C_Y2 = C_Y1 + 12 + 3                        # AI (after FY, notes, spacer)
LAST_COL = C_Y2 + 12 + 1                    # AV

LISTS = {
    "Review": ["Reviewed", "Not Reviewed"],
    "Status": ["Active", "Not Active", "Termed", "Contract Error", "New", "Old Contract"],
    "opportunity_stage": ["Closed Won"],
    "market": ["US", "Canada"],
    "Product Category": ["Amazon DSP Media", "Amazon Sponsored Ads", "Other DSP Media", "Other Marketplace Sponsored Ads",
                         "Retail Amazon", "Walmart Sponsored Ads"],
    "Pricing Structure": ["% of COGS", "% of Media (Fixed)", "% of Media (Tiered)", "% of Sales (Fixed)", "% of Sales (Tiered)"],
    "Currency": ["US Dollars"],
}
YELLOW = PatternFill("solid", fgColor="FFFFCC")
GREY = PatternFill("solid", fgColor="D9D9D9")
HEAD = PatternFill("solid", fgColor="1F3864")
SECTION = PatternFill("solid", fgColor="D9E1F2")
THIN = Border(*(Side(style="thin", color="BFBFBF"),) * 4)
NUM = "#,##0;(#,##0);-"
UNLOCKED = Protection(locked=False)


def file_token(team):
    return re.sub(r"[^A-Za-z0-9-]", "", team)  # "NA-North" -> "NA-North", "Unassigned" -> "Unassigned"


def tab_name(name, used):
    # Excel limits: 31 characters, no []:*?/\ ; Instructions and Lists are reserved
    n = re.sub(r"[\[\]:*?/\\]", "", name or "Account").replace(" Parent", "").strip(" '")[:28] or "Account"
    if n in ("Instructions", "Lists"):
        n = "Acct " + n
    base, k = n[:31], 2
    while base.lower() in used:
        base, k = f"{n[:27]} ({k})", k + 1
    used.add(base.lower())
    return base


def write_instructions(wb, team, leader, cycle, years, due_date, message):
    ws = wb.active
    ws.title = "Instructions"
    y1, y2 = years
    rows = [
        ("Team Volume Forecast", None),
        (None, None),
        ("Forecast", f"{cycle} – {team}" + (f" (team leader {leader})" if leader else "")),
        ("Please return by", due_date.strftime("%d %b %Y") if due_date else ""),
        (None, None),
        ("Objective & Structure :",
         "Teams input monthly volume forecasts associated with dynamic rate contracts for each account within your team. "
         "These volume inputs are used for the revenue forecast, specifically fee revenue from dynamic rate contracts."),
        (None, "Contract and product information is sourced directly from Salesforce; each tab also has a section to manually enter contracts "
               "and products not found in Salesforce. There is one tab per parent account."),
        (None, "The templates are pre-seeded with your previous forecast – please overwrite with any updates."),
        (None, None),
        ("Instructions :", "1. Go to each account tab and start with the second table, 'Salesforce Query' – the closed won products for the account."),
        (None, "   a. Use the Team Leader, Director and Preparer fields to find the rows you are responsible for; fill in any blank names."),
        (None, f"   b. Enter the {y1} and {y2} monthly volume forecasts in the yellow cells. The Pricing Structure (column I) says what to enter "
               "(e.g. media spend for '% of Media' rows)."),
        (None, "   c. Grey months are outside the contract's dates per Salesforce (or after the end date with no renewal) – no forecast is needed "
               "unless you know the work continues."),
        (None, "   d. Update the Review and Status fields once complete, and put your monthly assumptions in the Notes fields."),
        (None, "   e. The lowest level of forecasting is platform by market. Please total retailer-level forecasts by platform."),
        (None, "2. Then go to the first table, 'Manual Entries': update the rows already there and add any opportunity, product, platform or "
               "market you know is active but is missing from the Salesforce Query, filling in all yellow cells."),
        (None, "3. Do not rename tabs, change the white cells, or move the header rows – the file is read back automatically."),
        (None, "4. Save and close the workbook once complete."),
    ]
    if message:
        rows += [(None, None), ("Note from FP&A :", message)]
    for r in rows:
        ws.append(list(r))
    ws["A1"].font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 140
    for row in ws.iter_rows(min_row=2, max_col=2):
        row[0].font = Font(bold=True)
        row[1].alignment = Alignment(wrap_text=True, vertical="top")


def write_lists(wb):
    ws = wb.create_sheet("Lists")
    refs = {}
    for c, (name, values) in enumerate(LISTS.items(), start=1):
        ws.cell(row=1, column=c, value=name).font = Font(bold=True)
        for r, v in enumerate(values, start=2):
            ws.cell(row=r, column=c, value=v)
        col = get_column_letter(c)
        refs[name] = f"Lists!${col}$2:${col}${len(values) + 1}"
    ws.sheet_state = "hidden"
    return refs


def header_rows(ws, r, months):
    """Display header row r and technical header row r + 1 (the row the readers key on)."""
    display = [d for d, _, _ in INFO] + [d for d, _ in TEAM_INPUT]
    tech = [t for _, t, _ in INFO] + [d for d, _ in TEAM_INPUT]
    for y, col in ((0, C_Y1), (1, C_Y2)):
        ms = months[y * 12:(y + 1) * 12]
        labels = [m.strftime("%b-%y") for m in ms] + [f"FY {ms[0].year}", f"{ms[0].year} Notes [Specify Monthly Assumptions]"]
        for k, lab in enumerate(labels):
            for row, vals in ((r, display), (r + 1, tech)):
                ws.cell(row=row, column=col + k, value=lab)
    for c, (d, t) in enumerate(zip(display, tech), start=1):
        ws.cell(row=r, column=c, value=d)
        ws.cell(row=r + 1, column=c, value=t)
    for c in range(1, LAST_COL + 1):
        ws.cell(row=r, column=c).font = Font(bold=True, color="FFFFFF")
        ws.cell(row=r, column=c).fill = HEAD
        ws.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=r + 1, column=c).font = Font(size=8, color="808080")


def write_row(ws, r, row, months, manual, grey_months=frozenset()):
    for c, (_, _, key) in enumerate(INFO, start=1):
        v = row.get(key)
        cell = ws.cell(row=r, column=c, value=v)
        cell.border = THIN
        if isinstance(v, (dt.date, dt.datetime)):
            cell.number_format = "m/d/yyyy"
        if manual:
            cell.fill = YELLOW
            cell.protection = UNLOCKED
    for k, (_, key) in enumerate(TEAM_INPUT):
        cell = ws.cell(row=r, column=C_TEAM + k, value=row.get(key))
        cell.fill, cell.protection, cell.border = YELLOW, UNLOCKED, THIN
    for y, col in ((0, C_Y1), (1, C_Y2)):
        ms = months[y * 12:(y + 1) * 12]
        for k, m in enumerate(ms):
            cell = ws.cell(row=r, column=col + k, value=row.get("months", {}).get(m, 0))
            cell.number_format, cell.protection, cell.border = NUM, UNLOCKED, THIN
            cell.fill = GREY if m in grey_months else YELLOW
        fy = ws.cell(row=r, column=col + 12, value=f"=SUM({get_column_letter(col)}{r}:{get_column_letter(col + 11)}{r})")
        fy.number_format, fy.font = NUM, Font(bold=True)
        note = ws.cell(row=r, column=col + 13, value=row.get("notes", {}).get(ms[0].year))
        note.fill, note.protection, note.border = YELLOW, UNLOCKED, THIN


def build_account_tab(ws, parent, manual_rows, sf_rows, months, refs):
    ws["A1"] = "INFORMATION"
    ws["A1"].font = Font(bold=True)
    ws["A2"], ws["C2"] = "Account", "Cell Key"
    ws["C3"] = "White Cells - DO NOT EDIT"
    ws["C3"].border = THIN
    ws["A4"], ws["B4"], ws["C4"] = parent, parent, "Yellow Cells - INPUT"
    ws["C4"].fill = YELLOW
    ws["A6"] = "Product & Opportunity Information"
    ws["A7"] = "Do Not Edit - from Salesforce"
    for col in (C_TEAM, C_Y2):
        ws.cell(row=6, column=col, value="Team Forecast Input")
        ws.cell(row=7, column=col, value="Input Forecast & Other Information Into Yellow Cells")
    for c in (1, C_TEAM, C_Y2):
        ws.cell(row=6, column=c).font = Font(bold=True, size=12)

    # Manual Entries table
    ws["A8"] = "MANUAL ENTRIES - ENTER ANY PRODUCTS NOT FOUND IN SALESFORCE QUERY BELOW"
    ws["A8"].font = Font(bold=True)
    for k, m in enumerate(months):
        cell = ws.cell(row=8, column=(C_Y1 if k < 12 else C_Y2) + k % 12, value=m)
        cell.number_format = "mmm-yy"
    for c in range(1, LAST_COL + 1):
        ws.cell(row=8, column=c).fill = SECTION
    header_rows(ws, 9, months)
    r = 11
    blanks = max(10 - len(manual_rows), 5)
    for row in manual_rows + [{"parent_account": None}] * blanks:
        write_row(ws, r, row, months, manual=True)
        r += 1
    manual_range = (11, r - 1)
    ws.cell(row=r, column=1, value="Enter more manual rows above this line if needed ---").font = Font(italic=True)
    r += 2

    # Salesforce Query table
    ws.cell(row=r, column=1, value="SALESFORCE QUERY - DO NOT EDIT WHITE CELLS - DO NOT SORT").font = Font(bold=True)
    for c in range(1, LAST_COL + 1):
        ws.cell(row=r, column=c).fill = SECTION
    header_rows(ws, r + 1, months)
    sf_header = r + 2
    r += 3
    for row in sf_rows:
        write_row(ws, r, row, months, manual=False, grey_months=row["grey"])
        r += 1
    last = max(r - 1, sf_header)

    # Drop-downs
    def dv(list_name, ranges):
        v = DataValidation(type="list", formula1=refs[list_name], allow_blank=True)
        ws.add_data_validation(v)
        for rg in ranges:
            v.add(rg)
    team_review, team_status = get_column_letter(C_TEAM), get_column_letter(C_TEAM + 1)
    m0, m1 = manual_range
    dv("Review", [f"{team_review}{m0}:{team_review}{m1}", f"{team_review}{sf_header + 1}:{team_review}{max(last, sf_header + 1)}"])
    dv("Status", [f"{team_status}{m0}:{team_status}{m1}", f"{team_status}{sf_header + 1}:{team_status}{max(last, sf_header + 1)}"])
    dv("Product Category", [f"C{m0}:C{m1}"])
    dv("Pricing Structure", [f"I{m0}:I{m1}"])

    widths = {"A": 26, "B": 28, "C": 26, "D": 34, "E": 14, "F": 9, "G": 11, "H": 11, "I": 18, "J": 11, "K": 19, "L": 11, "M": 40, "N": 14}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for c in range(C_TEAM, C_TEAM + len(TEAM_INPUT)):
        ws.column_dimensions[get_column_letter(c)].width = 16
    for c in list(range(C_Y1, C_Y1 + 13)) + list(range(C_Y2, C_Y2 + 13)):
        ws.column_dimensions[get_column_letter(c)].width = 12
    for c in (C_Y1 + 13, C_Y2 + 13):
        ws.column_dimensions[get_column_letter(c)].width = 45
    ws.column_dimensions[get_column_letter(C_Y1 + 14)].width = 3
    ws.freeze_panes = "F1"
    ws.sheet_properties.tabColor = "FFFF00"
    p = ws.protection
    p.sheet = True
    p.formatCells = p.formatColumns = p.formatRows = p.insertRows = p.deleteRows = p.autoFilter = p.sort = False


def prefill(team, years):
    """Latest return for the team: values by PID × platform × market, and its manual rows."""
    last = spark.sql(
        f"""SELECT cycle, forecast_year, actual_months FROM {RETURNS} WHERE team = :team
            GROUP BY ALL ORDER BY forecast_year DESC NULLS LAST, actual_months DESC NULLS LAST LIMIT 1""",
        args={"team": team}).collect()
    if not last:
        return None, {}, defaultdict(list)
    l = last[0]
    pdf = spark.sql(
        f"""SELECT * FROM {RETURNS} WHERE team = :team AND cycle <=> :cycle AND forecast_year <=> :fy
            ORDER BY sheet_name, sheet_row, month""",
        args={"team": team, "cycle": l.cycle, "fy": l.forecast_year}).toPandas()
    by_key, manual = {}, defaultdict(list)
    for (sheet, sheet_row), g in pdf.groupby(["sheet_name", "sheet_row"], sort=False):
        first = {k: (None if v != v else v) for k, v in g.iloc[0].to_dict().items()}  # NaN -> None
        vals = {m: (0 if a != a or a is None else float(a)) for m, a in zip(g["month"], g["amount"])}
        notes = {}
        for m, n in zip(g["month"], g["notes"]):
            if n and n == n:
                notes.setdefault(m.year, n)
        row = {**first, "months": vals, "notes": notes, "sheet_name": sheet}
        if first["section"] == "manual":
            manual[first["parent_account"]].append(row)
            continue
        key = (first["pid"], first["platform"], first["market"])
        if key in by_key:  # same product on two rows: add up
            for m, v in vals.items():
                by_key[key]["months"][m] = by_key[key]["months"].get(m, 0) + v
        else:
            by_key[key] = row
    return l.cycle, by_key, manual


def build_team_workbook(path, team, leader, cycle, first_month, due_date, message):
    years = (first_month.year, first_month.year + 1)
    months = [dt.date(years[0] + k // 12, k % 12 + 1, 1) for k in range(24)]
    sf = spark.sql(
        f"""SELECT * FROM {SCHEMA}.rev_team_workbook_rows WHERE team = :team AND end_date >= :y0
            ORDER BY parent_account, account_name, product_category, pid, platform, market""",
        args={"team": team, "y0": months[0]}).toPandas()
    prev_cycle, prev, prev_manual = prefill(team, years)

    tabs = defaultdict(list)
    for rec in sf.to_dict("records"):
        rec = {k: (None if v != v else v) for k, v in rec.items()}
        old = prev.get((rec["pid"], rec["platform"], rec["market"]))
        start = rec["effective_date"].replace(day=1) if rec["effective_date"] else None
        grey = {m for m in months if (start and m < start)
                or (rec["end_date"] and not rec["next_opp_id"] and m > rec["end_date"])}
        tabs[rec["parent_account"]].append({
            **rec, "grey": grey, "review": "Not Reviewed", "team_leader": leader,
            "status": (old or {}).get("status") or ("New" if prev and not old else "Active"),
            "director": (old or {}).get("director"), "preparer": (old or {}).get("preparer"),
            "months": (old or {}).get("months", {}), "notes": (old or {}).get("notes", {}),
            "sheet_name": (old or {}).get("sheet_name"),
        })
    for parent, rows in prev_manual.items():
        tabs.setdefault(parent, [])
        for r in rows:
            r.update(review="Not Reviewed", team_leader=leader)

    wb = Workbook()
    write_instructions(wb, team, leader, cycle, years, due_date, message)
    refs = write_lists(wb)
    used = set()
    n_rows = 0
    for parent in sorted(tabs, key=lambda p: (p or "").lower()):
        sf_rows, manual_rows = tabs[parent], prev_manual.get(parent, [])
        names = Counter(r["sheet_name"] for r in sf_rows + manual_rows if r.get("sheet_name"))
        name = names.most_common(1)[0][0] if names else parent
        ws = wb.create_sheet(tab_name(name, used))
        build_account_tab(ws, parent, manual_rows, sf_rows, months, refs)
        n_rows += len(sf_rows) + len(manual_rows)
    wb.move_sheet("Lists", offset=len(wb.sheetnames))
    wb.save(path)
    return n_rows, len(tabs), prev_cycle

# COMMAND ----------

if forced_send_id:
    sends = spark.sql(f"SELECT * FROM {SCHEMA}.rev_send_schedule WHERE send_id = :id AND status <> 'cancelled'",
                      args={"id": forced_send_id}).collect()
else:
    sends = spark.sql(f"SELECT * FROM {SCHEMA}.rev_send_schedule WHERE status = 'scheduled' AND send_at <= current_timestamp() ORDER BY send_at").collect()
print(f"{len(sends)} send(s) due")

# Salesforce data checks first: no workbooks are built from Salesforce data that is not being read in full
check_error = None
if sends:
    try:
        dbutils.notebook.run(CHECKS_NOTEBOOK, 1800, {"schema": SCHEMA})
    except Exception as e:
        errs = spark.sql(f"""SELECT check_name, detail FROM {CHECK_LOG}
                             WHERE run_id = (SELECT run_id FROM {CHECK_RUNS} ORDER BY run_at DESC LIMIT 1) AND status = 'error'""").collect()
        check_error = ("Salesforce data checks failed – nothing built. "
                       + ("; ".join(f"{r.check_name}: {r.detail}" for r in errs) or f"{type(e).__name__}: {e}"))[:1000]
        print(check_error)

mail_cfg = graph_settings()
recipients = {r.team: r for r in spark.sql(f"SELECT * FROM {SCHEMA}.rev_team_recipients").collect()}

for s in sends:
    if check_error:
        log(s.send_id, "all", None, None, "failed", check_error)
        set_status(s.send_id, "failed")
        continue
    set_status(s.send_id, "running")
    failures = 0
    safe_cycle = re.sub(r"[^A-Za-z0-9+ _-]", "_", s.cycle_label)
    out_dir = f"{OUT_ROOT}/{safe_cycle}"
    os.makedirs(out_dir, exist_ok=True)
    for team in s.teams:
        try:
            rec = recipients.get(team)
            leader = rec.team_leader if rec else None
            fname = f"Team Volume Forecast_{file_token(team)}_{safe_cycle}.xlsx"
            tmp = os.path.join(tempfile.mkdtemp(), fname)
            n_rows, n_tabs, prev_cycle = build_team_workbook(tmp, team, leader, s.cycle_label, s.first_forecast_month, s.due_date, s.message)
            dest = f"{out_dir}/{fname}"
            shutil.copyfile(tmp, dest)
            seeded = f"{n_tabs} accounts, prefilled from {prev_cycle}" if prev_cycle else f"{n_tabs} accounts, no previous return to prefill"
            to = split_emails(rec.to_emails) if rec else []
            cc = split_emails(rec.cc_emails) if rec else []
            if rec is not None and rec.active is False:
                log(s.send_id, team, dest, n_rows, "built", f"{seeded}; team inactive in recipients - not emailed")
            elif mail_cfg and to:
                due = s.due_date.strftime("%d %b %Y") if s.due_date else "the due date"
                body = (f"Hi {leader or ''},\n\nAttached is the {s.cycle_label} Team Volume Forecast for {team} ({n_tabs} accounts). "
                        f"It is pre-seeded with your last forecast – please update the yellow cells and return it by {due}.\n\n"
                        + (f"{s.message}\n\n" if s.message else "") + "Thanks,\nFP&A")
                send_mail(mail_cfg, to, cc, f"{s.cycle_label} Team Volume Forecast – {team}", body, tmp)
                log(s.send_id, team, dest, n_rows, "emailed", f"{seeded}; " + ", ".join(to + cc))
            else:
                log(s.send_id, team, dest, n_rows, "email not configured" if not mail_cfg else "no recipient",
                    f"{seeded}; file is in the volume – download it from the app")
        except Exception as e:
            failures += 1
            log(s.send_id, team, None, None, "failed", f"{type(e).__name__}: {e}"[:1000])
    set_status(s.send_id, "failed" if failures == len(s.teams) else "sent")
    print(f"{s.cycle_label}: {len(s.teams)} teams, {failures} failed")
