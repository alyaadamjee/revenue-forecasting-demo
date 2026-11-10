-- Revenue forecasting demo: team send tables (recipients, schedule, log), the Salesforce filter list,
-- the team workbook rows, and the Salesforce data check logs.

CREATE TABLE IF NOT EXISTS {{schema}}.rev_team_recipients (
  team STRING NOT NULL COMMENT 'Team code (Unassigned = no team)',
  team_leader STRING,
  to_emails STRING COMMENT 'Comma-separated addresses the workbook goes to',
  cc_emails STRING COMMENT 'Comma-separated addresses copied',
  sharepoint_folder STRING COMMENT 'Team folder on SharePoint/OneDrive (optional)',
  active BOOLEAN COMMENT 'false = never sent to',
  updated_by STRING,
  updated_at TIMESTAMP)
COMMENT 'Who receives each team''s workbook. Seeded once from src_teams; edit in the app (Recipients tab).';

MERGE INTO {{schema}}.rev_team_recipients t
USING (SELECT team, team_leader, to_emails FROM {{schema}}.src_teams
       UNION ALL SELECT 'Unassigned', NULL, NULL) s
ON t.team = s.team
WHEN NOT MATCHED THEN INSERT (team, team_leader, to_emails, cc_emails, sharepoint_folder, active, updated_by, updated_at)
  VALUES (s.team, s.team_leader, s.to_emails, NULL, NULL, true, 'seed', current_timestamp());

CREATE TABLE IF NOT EXISTS {{schema}}.rev_send_schedule (
  send_id STRING NOT NULL,
  cycle_label STRING NOT NULL COMMENT 'e.g. 10+2 = 10 months actuals + 2 forecast',
  first_forecast_month DATE NOT NULL COMMENT 'First month teams forecast; earlier months are history',
  send_at TIMESTAMP NOT NULL COMMENT 'When the job builds and sends the workbooks (UTC)',
  due_date DATE,
  teams ARRAY<STRING> NOT NULL,
  message STRING,
  status STRING NOT NULL COMMENT 'scheduled / running / sent / cancelled / failed',
  created_by STRING,
  created_at TIMESTAMP,
  updated_at TIMESTAMP)
COMMENT 'Team sends scheduled in the app; the team send job picks up rows with status scheduled and send_at in the past.';

CREATE TABLE IF NOT EXISTS {{schema}}.rev_send_log (
  send_id STRING NOT NULL,
  team STRING NOT NULL,
  file_path STRING,
  line_count INT,
  status STRING COMMENT 'built / emailed / email not configured / no recipient / failed',
  detail STRING,
  logged_at TIMESTAMP)
COMMENT 'One row per send and team, written by the team send job.';

-- Every value of the filtered Salesforce fields, marked in scope or not. The workbook view takes its filters from here and
-- the data checks error on any value in neither list, so a new or renamed value is never silently dropped.
-- To accept a new value: insert it with in_scope true (it appears in the workbooks) or false (ignored), then rerun the checks.
CREATE OR REPLACE TABLE {{schema}}.rev_sf_filter_values (
  field STRING COMMENT 'opp_stage, pricing_structure or product_category',
  value STRING COMMENT 'Salesforce value; (blank) = null or empty',
  in_scope BOOLEAN COMMENT 'true = included in the team workbooks, false = known and deliberately left out',
  note STRING,
  updated_at TIMESTAMP
) COMMENT 'Filter values for the team workbook rows (view rev_team_workbook_rows). Values not listed here make the Salesforce data checks fail.';

INSERT INTO {{schema}}.rev_sf_filter_values
SELECT field, value, in_scope, note, current_timestamp() FROM VALUES
  ('opp_stage', 'Closed Won', true, 'workbook filter'),
  ('opp_stage', 'Closed Lost', false, NULL), ('opp_stage', 'Discovery', false, NULL),
  ('opp_stage', 'Qualification & Solution Design', false, NULL), ('opp_stage', 'Proposal', false, NULL),
  ('opp_stage', 'Negotiation', false, NULL), ('opp_stage', 'Final Approval', false, NULL),
  ('pricing_structure', '% of COGS', true, 'workbook filter'),
  ('pricing_structure', '% of Media (Fixed)', true, 'workbook filter'), ('pricing_structure', '% of Media (Tiered)', true, 'workbook filter'),
  ('pricing_structure', '% of Sales (Fixed)', true, 'workbook filter'), ('pricing_structure', '% of Sales (Tiered)', true, 'workbook filter'),
  ('pricing_structure', 'Retainer', false, 'flat fee - no forecast input needed'), ('pricing_structure', 'One-Off', false, NULL),
  ('pricing_structure', '(blank)', false, 'opportunities without products'),
  ('product_category', 'Amazon DSP Media', true, 'workbook filter'), ('product_category', 'Amazon Sponsored Ads', true, 'workbook filter'),
  ('product_category', 'Other DSP Media', true, 'workbook filter'), ('product_category', 'Other Marketplace Sponsored Ads', true, 'workbook filter'),
  ('product_category', 'Retail Amazon', true, 'workbook filter'), ('product_category', 'Walmart Sponsored Ads', true, 'workbook filter'),
  ('product_category', 'Consultancy', false, NULL), ('product_category', 'Content', false, NULL),
  ('product_category', '(blank)', false, 'opportunities without products')
  AS t(field, value, in_scope, note);

CREATE OR REPLACE VIEW {{schema}}.rev_team_workbook_rows
COMMENT 'Team workbook Salesforce rows, one per PID x platform x market: won dynamic-rate products (filters in rev_sf_filter_values), NA teams US/Canada or blank market, EU teams all markets. The latest contract term wins when a PID repeats.'
AS
WITH f AS (SELECT field, value FROM {{schema}}.rev_sf_filter_values WHERE in_scope),
p AS (
  SELECT *
  FROM {{schema}}.src_sf_opportunity_products
  WHERE opp_stage IN (SELECT value FROM f WHERE field = 'opp_stage')
    AND pricing_structure IN (SELECT value FROM f WHERE field = 'pricing_structure')
    AND product_category IN (SELECT value FROM f WHERE field = 'product_category')
    AND (team LIKE 'EU-%' OR market IN ('US', 'Canada') OR nullif(trim(market), '') IS NULL)
)
SELECT coalesce(team, 'Unassigned') AS team,
       parent_account AS parent_account,
       product_id AS pid, platform AS platform, market AS market,
       max_by(acc_name, opp_contract_end_date) AS account_name,
       max_by(product_category, opp_contract_end_date) AS product_category,
       max_by(product_name, opp_contract_end_date) AS product_name,
       max_by(opp_effective_date, opp_contract_end_date) AS effective_date,
       max(opp_contract_end_date) AS end_date,
       max_by(pricing_structure, opp_contract_end_date) AS pricing_structure,
       max_by(opp_currency, opp_contract_end_date) AS currency,
       max_by(opp_code, opp_contract_end_date) AS opportunity_id,
       max_by(opp_name, opp_contract_end_date) AS opportunity_name,
       max_by(opp_stage, opp_contract_end_date) AS opportunity_stage,
       max_by(opp_next_opp_id, opp_contract_end_date) AS next_opp_id
FROM p
GROUP BY ALL;

CREATE TABLE IF NOT EXISTS {{schema}}.rev_sf_check_runs (
  run_id STRING, run_at TIMESTAMP, source_refreshed_at TIMESTAMP, source_rows BIGINT, template_rows BIGINT,
  errors INT, warnings INT, status STRING COMMENT 'ok, warn or error'
) COMMENT 'One row per run of the Salesforce data checks notebook.';

CREATE TABLE IF NOT EXISTS {{schema}}.rev_sf_check_log (
  run_id STRING, run_at TIMESTAMP, check_name STRING, status STRING COMMENT 'ok, warn or error', row_count BIGINT, detail STRING
) COMMENT 'Result of each Salesforce data check per run.';

CREATE TABLE IF NOT EXISTS {{schema}}.rev_sf_check_snapshot (
  run_id STRING, run_at TIMESTAMP, team STRING, pid STRING, platform STRING, market STRING, end_date DATE
) COMMENT 'Team workbook rows at each check run (last 30 runs), to spot rows and teams that disappear.';

CREATE OR REPLACE VIEW {{schema}}.rev_sf_check_latest
COMMENT 'The latest Salesforce data check run and its non-ok results (banner in the app).'
AS
SELECT r.run_id, r.run_at, r.source_refreshed_at, r.status AS run_status, l.check_name, l.status, l.row_count, l.detail
FROM (SELECT * FROM {{schema}}.rev_sf_check_runs ORDER BY run_at DESC LIMIT 1) r
LEFT JOIN {{schema}}.rev_sf_check_log l ON l.run_id = r.run_id AND l.status <> 'ok';
