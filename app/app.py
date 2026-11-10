"""Revenue Team Send (demo) - schedule the monthly dynamic-charge workbooks to the teams.

Tables (schema in env REV_SCHEMA): rev_send_schedule, rev_send_log, rev_team_recipients, rev_team_workbook_rows.
The team send job (notebook Revenue Demo - Team Send) builds and sends the workbooks; this app only schedules, monitors and downloads.
"""
import datetime as dt
import os
import uuid

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sql.exc import RequestError
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

SCHEMA = os.environ.get("REV_SCHEMA", "main.revenue_demo")  # the app service principal needs USE CATALOG / USE SCHEMA + table grants here
JOB_ID = int(os.environ["SEND_JOB_ID"])
cfg = Config()

st.set_page_config(page_title="Revenue Team Send", page_icon="📤", layout="wide")


def user_email():
    return st.context.headers.get("X-Forwarded-Email") or "unknown"


@st.cache_resource
def connection():
    # The app's own service principal; it has grants on the app tables only.
    return sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}",
        credentials_provider=lambda: cfg.authenticate,
    )


def run(q, params, fetch):
    # The cached connection's session expires when the app sits idle; reconnect once and retry.
    for attempt in range(2):
        try:
            with connection().cursor() as c:
                c.execute(q, params or {})
                return c.fetchall_arrow().to_pandas() if fetch else None
        except RequestError:
            if attempt:
                raise
            connection.clear()


def query(q, params=None):
    return run(q, params, fetch=True)


def execute(q, params=None):
    run(q, params, fetch=False)


def next_month(d):
    return dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)


st.title("Revenue Team Send")
st.info("Demo – all clients, people and amounts are fake.")
st.caption("Schedule the Team Volume Forecast workbooks to the teams. The workbooks are built from Salesforce and each team's last return "
           "on the send date; until email is set up they wait in the volume for download.")

try:
    chk = query(f"SELECT * FROM {SCHEMA}.rev_sf_check_latest")
except Exception:
    chk = pd.DataFrame()
if not chk.empty:
    latest = chk.iloc[0]
    issues = chk.dropna(subset=["check_name"])[["check_name", "status", "row_count", "detail"]]
    when = f"Salesforce data checks, {latest.run_at:%d %b %H:%M} UTC (data refreshed {latest.source_refreshed_at:%d %b %H:%M})"
    if latest.run_status == "error":
        st.error(f"{when}: **failed** – sends are blocked until this is fixed in Salesforce or in rev_sf_filter_values.")
    elif latest.run_status == "warn":
        st.warning(f"{when}: passed with warnings.")
    else:
        st.caption(f"{when}: all passed.")
    if not issues.empty:
        with st.expander("Check details"):
            st.dataframe(issues.sort_values("status"), hide_index=True, use_container_width=True)

tab_new, tab_sends, tab_recipients = st.tabs(["Schedule a send", "Sends", "Recipients"])

# --- Schedule a send -------------------------------------------------------------------------
with tab_new:
    today = dt.date.today()
    c1, c2, c3 = st.columns(3)
    first_month = c1.date_input("First month to forecast", value=next_month(today).replace(day=1),
                                help="Earlier months appear as locked history in the workbook.").replace(day=1)
    cycle = c2.text_input("Cycle", value=f"{first_month.month - 1}+{13 - first_month.month}",
                          help="Label used in file names and emails, e.g. 10+2")
    due = c3.date_input("Return by", value=today + dt.timedelta(days=7))
    c4, c5 = st.columns(2)
    send_date = c4.date_input("Send on", value=today)
    send_time = c5.time_input("Send at (UTC)", value=dt.time(8, 0),
                              help="The job runs on the hour, so the send goes out at the first full hour after this time.")

    year_start = first_month.replace(month=1)
    counts = query(
        f"""SELECT r.team, r.team_leader, r.to_emails, r.active,
                   count(t.pid) AS lines, count(DISTINCT t.pid) AS pids, count(DISTINCT t.parent_account) AS accounts
            FROM {SCHEMA}.rev_team_recipients r
            LEFT JOIN {SCHEMA}.rev_team_workbook_rows t ON t.team = r.team AND t.end_date >= :y0
            GROUP BY ALL ORDER BY r.team""",
        {"y0": year_start},
    )
    st.subheader("Teams")
    st.caption("Lines = Salesforce Query rows in the team's Team Volume Forecast workbook (closed won dynamic-rate products, "
               "by platform and market, ending in or after the forecast year). Each account gets its own tab; manual rows "
               "and values are carried over from the team's last return.")
    counts.insert(0, "send", counts["active"].fillna(True) & (counts["lines"] > 0))
    edited = st.data_editor(
        counts, hide_index=True, use_container_width=True, key="team_pick",
        column_config={"send": st.column_config.CheckboxColumn("Send"), "to_emails": "Sent to",
                       "team_leader": "Leader", "active": None},
        disabled=["team", "team_leader", "to_emails", "lines", "pids", "accounts"],
    )
    chosen = edited.loc[edited["send"], "team"].tolist()
    missing = edited.loc[edited["send"] & edited["to_emails"].isna(), "team"].tolist()
    if missing:
        st.warning(f"No recipient yet for {', '.join(missing)} – add them on the Recipients tab, or their files are only saved for download.")

    with st.expander("Preview a team's lines"):
        pick = st.selectbox("Team", chosen or counts["team"].tolist())
        if pick:
            prev = query(
                f"""SELECT parent_account, account_name, product_category, product_name, platform, market, pid, opportunity_id,
                           pricing_structure, effective_date, end_date
                    FROM {SCHEMA}.rev_team_workbook_rows
                    WHERE team = :sp AND end_date >= :y0
                    ORDER BY parent_account, account_name, product_category, pid, platform, market""",
                {"y0": year_start, "sp": pick},
            )
            st.dataframe(prev, hide_index=True, use_container_width=True)

    message = st.text_area("Note to include (optional)")
    if st.button("Schedule send", type="primary", disabled=not chosen or not cycle.strip()):
        send_at = dt.datetime.combine(send_date, send_time)
        execute(
            f"""INSERT INTO {SCHEMA}.rev_send_schedule
                VALUES (:id, :cycle, :fm, :send_at, :due, from_json(:teams, 'array<string>'), :msg, 'scheduled', :by,
                        current_timestamp(), current_timestamp())""",
            {"id": str(uuid.uuid4()), "cycle": cycle.strip(), "fm": first_month, "send_at": send_at, "due": due,
             "teams": pd.Series(chosen).to_json(orient="values"), "msg": message or None, "by": user_email()},
        )
        st.success(f"Scheduled {cycle} to {len(chosen)} teams for {send_at:%d %b %Y %H:%M} UTC.")

# --- Sends -----------------------------------------------------------------------------------
with tab_sends:
    sends = query(f"""SELECT send_id, cycle_label, first_forecast_month, send_at, due_date, size(teams) AS teams, status, created_by, created_at
                      FROM {SCHEMA}.rev_send_schedule ORDER BY created_at DESC LIMIT 50""")
    if sends.empty:
        st.info("Nothing scheduled yet.")
    else:
        st.dataframe(sends.drop(columns="send_id"), hide_index=True, use_container_width=True)
        labels = {f"{r.cycle_label} – {r.send_at:%d %b %H:%M} – {r.status}": r.send_id for r in sends.itertuples()}
        sel = labels[st.selectbox("Send", list(labels))]
        row = sends.loc[sends["send_id"] == sel].iloc[0]
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Send now", disabled=row.status not in ("scheduled", "failed")):
            execute(f"UPDATE {SCHEMA}.rev_send_schedule SET send_at = current_timestamp(), status = 'scheduled', updated_at = current_timestamp() WHERE send_id = :id", {"id": sel})
            WorkspaceClient().jobs.run_now(job_id=JOB_ID, notebook_params={"send_id": sel})
            st.success("Started – refresh in a few minutes.")
        if b2.button("Cancel", disabled=row.status != "scheduled"):
            execute(f"UPDATE {SCHEMA}.rev_send_schedule SET status = 'cancelled', updated_at = current_timestamp() WHERE send_id = :id", {"id": sel})
            st.success("Cancelled.")
        log = query(f"SELECT team, line_count, status, detail, file_path, logged_at FROM {SCHEMA}.rev_send_log WHERE send_id = :id ORDER BY team", {"id": sel})
        st.dataframe(log.drop(columns="file_path"), hide_index=True, use_container_width=True)
        files = log.dropna(subset=["file_path"])
        if not files.empty:
            st.caption("Download")
            w = WorkspaceClient()
            cols = st.columns(4)
            for i, f in enumerate(files.itertuples()):
                data = w.files.download(f.file_path).contents.read()
                cols[i % 4].download_button(f.team, data, file_name=os.path.basename(f.file_path), key=f"dl-{sel}-{f.team}")

# --- Recipients ------------------------------------------------------------------------------
with tab_recipients:
    st.caption("Who gets each team's workbook. Separate several addresses with commas. Untick Active to stop sending to a team.")
    rec = query(f"SELECT team, team_leader, to_emails, cc_emails, sharepoint_folder, active, updated_by, updated_at FROM {SCHEMA}.rev_team_recipients ORDER BY team")
    new = st.data_editor(rec, hide_index=True, use_container_width=True, num_rows="dynamic", disabled=["updated_by", "updated_at"], key="rec_edit")
    if st.button("Save recipients"):
        for r in new.itertuples():
            if not r.team:
                continue
            execute(
                f"""MERGE INTO {SCHEMA}.rev_team_recipients t
                    USING (SELECT :team AS team) s ON t.team = s.team
                    WHEN MATCHED AND NOT (t.team_leader <=> :leader AND t.to_emails <=> :to AND t.cc_emails <=> :cc
                                          AND t.sharepoint_folder <=> :sp AND t.active <=> :active)
                      THEN UPDATE SET team_leader = :leader, to_emails = :to, cc_emails = :cc, sharepoint_folder = :sp,
                                      active = :active, updated_by = :by, updated_at = current_timestamp()
                    WHEN NOT MATCHED THEN INSERT (team, team_leader, to_emails, cc_emails, sharepoint_folder, active, updated_by, updated_at)
                      VALUES (:team, :leader, :to, :cc, :sp, :active, :by, current_timestamp())""",
                {"team": r.team, "leader": r.team_leader or None, "to": r.to_emails or None, "cc": r.cc_emails or None,
                 "sp": r.sharepoint_folder or None, "active": bool(r.active) if r.active is not None else True, "by": user_email()},
            )
        st.success("Saved.")
