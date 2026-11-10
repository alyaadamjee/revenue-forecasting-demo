# Databricks notebook source
# MAGIC %md
# MAGIC # Revenue Demo - Salesforce Data Checks
# MAGIC Checks that the Salesforce data behind the team workbooks (`src_sf_opportunity_products` →
# MAGIC `rev_team_workbook_rows`; fake data in the demo) is still being read in full. Every run is logged; **any error fails the notebook**, so the
# MAGIC daily job emails the owner and the *Revenue – Team Send* notebook (which runs this first) builds no workbooks.
# MAGIC
# MAGIC | Check | Fails (error) when | Warns when |
# MAGIC |---|---|---|
# MAGIC | `columns` | a column the workbooks use is missing from the source | |
# MAGIC | `freshness` | the source was last refreshed more than `max_age_hours` ago (36 in the live project) | |
# MAGIC | `source_rows` | the source has 10%+ fewer rows than at the last run | |
# MAGIC | `new_values` | a stage, pricing structure or product category is in neither list of `rev_sf_filter_values` | |
# MAGIC | `blank_market` | | an in-scope NA product has no market (still included, with Market blank) |
# MAGIC | `unknown_team` | an in-scope product's team code is not in `rev_team_recipients` | |
# MAGIC | `team_rows` | a team loses all its rows, or 20%+ (and 5+ rows), or all teams together lose 10%+ | |
# MAGIC | `other_market` | | an NA team product is in a market other than US / Canada (left out by the template rule) |
# MAGIC | `blank_platform` | | a workbook row has no platform |
# MAGIC | `lost_rows` | | a row still in contract at the last run is no longer in the workbook rows |
# MAGIC
# MAGIC Fix an error at the source (Salesforce), or – for `new_values` – add the value to `rev_sf_filter_values` (in_scope true or
# MAGIC false), then rerun. Results: `rev_sf_check_runs`, `rev_sf_check_log`; snapshots of the workbook rows: `rev_sf_check_snapshot`.

# COMMAND ----------

import datetime as dt
import uuid

dbutils.widgets.text("schema", "main.revenue_demo")
dbutils.widgets.text("max_age_hours", "8760")  # demo data is static; the live project uses 36
DM = APP = dbutils.widgets.get("schema")
SOURCE = f"{DM}.src_sf_opportunity_products"
REQUIRED_COLUMNS = ["opp_stage", "pricing_structure", "product_category", "team", "market", "platform",
                    "parent_account", "product_id", "acc_name", "product_name", "opp_effective_date",
                    "opp_contract_end_date", "opp_currency", "opp_code", "opp_name", "opp_next_opp_id", "refreshed_at"]
FILTERED_FIELDS = ["opp_stage", "pricing_structure", "product_category"]
MAX_AGE_HOURS = float(dbutils.widgets.get("max_age_hours"))
SOURCE_DROP, TEAM_DROP, TEAM_DROP_MIN_ROWS, TOTAL_DROP = 0.10, 0.20, 5, 0.10
KEEP_SNAPSHOTS = 30

run_id, run_at = str(uuid.uuid4()), dt.datetime.utcnow()
year_start = dt.date(run_at.year, 1, 1)
results = []  # (check_name, status, row_count, detail)


def check(name, status, rows=None, detail=None):
    results.append((name, status, rows, detail))
    print(f"[{status.upper():5}] {name}: {detail or ''}")


def listing(rows, fmt, limit=15):
    lines = [fmt(r) for r in rows[:limit]]
    return "; ".join(lines) + (f"; … {len(rows) - limit} more" if len(rows) > limit else "")


prev = spark.sql(f"SELECT * FROM {DM}.rev_sf_check_runs ORDER BY run_at DESC LIMIT 1").collect()
prev = prev[0] if prev else None

# COMMAND ----------

# columns
cols = {r.col_name for r in spark.sql(f"DESCRIBE TABLE {SOURCE}").collect()}
missing = [c for c in REQUIRED_COLUMNS if c not in cols]
check("columns", "error" if missing else "ok", len(missing), f"missing: {', '.join(missing)}" if missing else None)

if not missing:
    # freshness and size
    s = spark.sql(f"SELECT max(refreshed_at) AS refreshed, count(*) AS n FROM {SOURCE}").first()
    refreshed, source_rows = s.refreshed, s.n
    age = (run_at - refreshed).total_seconds() / 3600 if refreshed else None
    check("freshness", "error" if age is None or age > MAX_AGE_HOURS else "ok", None,
          f"last refreshed {refreshed:%Y-%m-%d %H:%M} UTC ({age:.0f}h ago)" if refreshed else "no refresh time")
    drop = 1 - source_rows / prev.source_rows if prev and prev.source_rows else 0
    check("source_rows", "error" if drop >= SOURCE_DROP else "ok", source_rows,
          f"{source_rows:,} rows" + (f" vs {prev.source_rows:,} at the last run ({-drop:+.1%})" if prev else ""))

    # new_values: values in neither list of rev_sf_filter_values (stage over all rows; the others over won rows)
    new = spark.sql(f"""
        WITH s AS (SELECT * FROM {SOURCE}),
        won AS (SELECT * FROM s WHERE opp_stage IN (SELECT value FROM {DM}.rev_sf_filter_values WHERE field = 'opp_stage' AND in_scope)),
        v AS (
          SELECT 'opp_stage' AS field, coalesce(nullif(trim(opp_stage), ''), '(blank)') AS value FROM s
          UNION ALL SELECT 'pricing_structure', coalesce(nullif(trim(pricing_structure), ''), '(blank)') FROM won
          UNION ALL SELECT 'product_category', coalesce(nullif(trim(product_category), ''), '(blank)') FROM won)
        SELECT v.field, v.value, count(*) AS n
        FROM v LEFT ANTI JOIN {DM}.rev_sf_filter_values f ON f.field = v.field AND f.value = v.value
        GROUP BY ALL ORDER BY n DESC""").collect()
    check("new_values", "error" if new else "ok", sum(r.n for r in new),
          listing(new, lambda r: f"{r.field} '{r.value}' ({r.n} rows)") if new else None)

    # In-scope source rows (stage, pricing, category in scope) live this year or later, for the drop checks
    spark.sql(f"""
        CREATE OR REPLACE TEMP VIEW in_scope AS
        WITH f AS (SELECT field, value FROM {DM}.rev_sf_filter_values WHERE in_scope)
        SELECT coalesce(team, 'Unassigned') AS team, parent_account AS parent_account,
               product_id AS pid, platform AS platform, nullif(trim(market), '') AS market, opp_code
        FROM {SOURCE}
        WHERE opp_stage IN (SELECT value FROM f WHERE field = 'opp_stage')
          AND pricing_structure IN (SELECT value FROM f WHERE field = 'pricing_structure')
          AND product_category IN (SELECT value FROM f WHERE field = 'product_category')
          AND opp_contract_end_date >= DATE'{year_start}'""")

    blank = spark.sql("SELECT team, parent_account, pid, opp_code FROM in_scope WHERE market IS NULL AND team NOT LIKE 'EU-%' "
                      "ORDER BY team, parent_account").collect()
    check("blank_market", "warn" if blank else "ok", len(blank),
          listing(blank, lambda r: f"{r.team} {r.parent_account} {r.pid} ({r.opp_code})") if blank else None)

    unknown = spark.sql(f"""SELECT team, count(*) AS n, min(parent_account) AS example FROM in_scope
                            WHERE team <> 'Unassigned' AND team NOT IN (SELECT team FROM {APP}.rev_team_recipients)
                            GROUP BY ALL ORDER BY n DESC""").collect()
    check("unknown_team", "error" if unknown else "ok", sum(r.n for r in unknown),
          listing(unknown, lambda r: f"'{r.team}' ({r.n} rows, e.g. {r.example})") if unknown else None)

    other = spark.sql("""SELECT team, market, count(*) AS n FROM in_scope
                         WHERE market IS NOT NULL AND team NOT LIKE 'EU-%' AND market NOT IN ('US', 'Canada')
                         GROUP BY ALL ORDER BY n DESC""").collect()
    check("other_market", "warn" if other else "ok", sum(r.n for r in other),
          listing(other, lambda r: f"{r.team} {r.market} ({r.n})") if other else None)

    # Workbook rows now vs the last run
    spark.sql(f"""CREATE OR REPLACE TEMP VIEW now_rows AS
                  SELECT team, pid, platform, market, end_date FROM {DM}.rev_team_workbook_rows WHERE end_date >= DATE'{year_start}'""")
    template_rows = spark.table("now_rows").count()
    no_platform = spark.sql("SELECT team, pid FROM now_rows WHERE nullif(trim(platform), '') IS NULL ORDER BY team").collect()
    check("blank_platform", "warn" if no_platform else "ok", len(no_platform),
          listing(no_platform, lambda r: f"{r.team} {r.pid}") if no_platform else None)

    if prev:
        spark.sql(f"""CREATE OR REPLACE TEMP VIEW prev_rows AS
                      SELECT team, pid, platform, market, end_date FROM {DM}.rev_sf_check_snapshot
                      WHERE run_id = '{prev.run_id}' AND end_date >= DATE'{year_start}'""")
        teams = spark.sql("""
            SELECT coalesce(p.team, n.team) AS team, coalesce(p.rows, 0) AS before, coalesce(n.rows, 0) AS now
            FROM (SELECT team, count(*) AS rows FROM prev_rows GROUP BY 1) p
            FULL JOIN (SELECT team, count(*) AS rows FROM now_rows GROUP BY 1) n ON p.team = n.team""").collect()
        bad = [r for r in teams if r.before and (r.now == 0 or (r.before - r.now >= TEAM_DROP_MIN_ROWS
                                                               and (r.before - r.now) / r.before >= TEAM_DROP))]
        before, now = sum(r.before for r in teams), sum(r.now for r in teams)
        total_bad = before and (before - now) / before >= TOTAL_DROP
        check("team_rows", "error" if bad or total_bad else "ok", now,
              (listing(bad, lambda r: f"{r.team} {r.before} → {r.now}") + "; " if bad else "")
              + f"all teams {before:,} → {now:,}")
        lost = spark.sql(f"""SELECT p.team, p.pid, p.platform, p.market FROM prev_rows p
                             LEFT ANTI JOIN now_rows n ON n.team = p.team AND n.pid <=> p.pid AND n.platform <=> p.platform AND n.market <=> p.market
                             WHERE p.end_date >= current_date() ORDER BY p.team, p.pid""").collect()
        check("lost_rows", "warn" if lost else "ok", len(lost),
              listing(lost, lambda r: f"{r.team} {r.pid} {r.platform} {r.market}") if lost else None)
    else:
        check("team_rows", "ok", template_rows, "first run – nothing to compare with yet")
else:
    refreshed = source_rows = template_rows = None

# COMMAND ----------

errors = sum(1 for r in results if r[1] == "error")
warnings = sum(1 for r in results if r[1] == "warn")
status = "error" if errors else "warn" if warnings else "ok"
spark.createDataFrame([(run_id, run_at, *r) for r in results],
                      "run_id string, run_at timestamp, check_name string, status string, row_count bigint, detail string") \
    .write.mode("append").saveAsTable(f"{DM}.rev_sf_check_log")
spark.createDataFrame([(run_id, run_at, refreshed, source_rows, template_rows, errors, warnings, status)],
                      "run_id string, run_at timestamp, source_refreshed_at timestamp, source_rows bigint, template_rows bigint, "
                      "errors int, warnings int, status string") \
    .write.mode("append").saveAsTable(f"{DM}.rev_sf_check_runs")
if template_rows is not None:
    spark.sql(f"""INSERT INTO {DM}.rev_sf_check_snapshot
                  SELECT '{run_id}', TIMESTAMP'{run_at:%Y-%m-%d %H:%M:%S}', team, pid, platform, market, end_date FROM {DM}.rev_team_workbook_rows""")
    spark.sql(f"""DELETE FROM {DM}.rev_sf_check_snapshot WHERE run_id NOT IN (
                    SELECT run_id FROM {DM}.rev_sf_check_runs WHERE template_rows IS NOT NULL ORDER BY run_at DESC LIMIT {KEEP_SNAPSHOTS})""")

summary = f"Salesforce data checks {status.upper()}: {errors} error(s), {warnings} warning(s) – run {run_id}"
print(summary)
if errors:
    raise Exception(summary + " | " + " | ".join(f"{n}: {d}" for n, s, _, d in results if s == "error"))
dbutils.notebook.exit(summary)
