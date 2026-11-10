-- Revenue forecasting demo: weighted open-pipeline forecast from Salesforce opportunities.

CREATE TABLE IF NOT EXISTS {{schema}}.rev_settings (
  key STRING NOT NULL,
  value STRING,
  note STRING,
  updated_at TIMESTAMP)
COMMENT 'Revenue forecasting settings, user-editable. Rows are inserted once and never overwritten by the build.';

MERGE INTO {{schema}}.rev_settings t
USING (SELECT * FROM VALUES
  ('best_case_min_probability', '60', 'Best Case deals count only at this probability or above'),
  ('commit_weight', '1', 'Share of a Commit deal counted. 1 = full amount; set to probability to weight like Best Case'),
  ('forecast_first_month', '2025-01-01', 'First month of the monthly spread'),
  ('forecast_last_month', '2027-12-01', 'Last month of the monthly spread'),
  ('budget_year_new_starts_only', '2027', 'In this year only deals whose accounting start is in the same year count. Blank = off')
  AS v(key, value, note)) s
ON t.key = s.key
WHEN NOT MATCHED THEN INSERT (key, value, note, updated_at) VALUES (s.key, s.value, s.note, current_timestamp());

CREATE OR REPLACE VIEW {{schema}}.rev_pipeline_opps
COMMENT 'Open Salesforce opportunities in the forecast, one row per deal: still open, forecast category Commit, or Best Case at best_case_min_probability or above. Pipeline category is not forecast. forecast_amount = 12-month incremental bookings x weight (Commit: commit_weight; Best Case: probability). Accounting start: contract effective date (else revenue start date) rounded to a month start, from the 16th rolls to next month, Best Case one month later again.'
AS
WITH st AS (SELECT map_from_entries(collect_list(struct(key, value))) AS m FROM {{schema}}.rev_settings),
o AS (
  SELECT o.*, coalesce(o.opp_effective_date, o.opp_rev_start_date) AS start_basis
  FROM {{schema}}.src_sf_opportunity o, st
  WHERE o.opp_stage NOT IN ('Closed Won', 'Closed Lost')
    AND (o.opp_forecast_category = 'Commit'
         OR (o.opp_forecast_category = 'Best Case' AND o.opp_probability >= CAST(st.m['best_case_min_probability'] AS DOUBLE))))
SELECT
  o.opp_code, o.opp_name, o.opp_type, o.opp_stage, o.opp_forecast_category, o.opp_probability,
  o.opp_close_date, o.opp_effective_date, o.opp_rev_start_date, o.opp_days_of_revenue,
  o.acc_name, o.team, o.team_leader, o.opp_team_bucket, o.opp_owner_name,
  o.opp_incremental_bookings_amount AS incremental_amount_12m,
  CASE WHEN o.opp_forecast_category = 'Commit'
       THEN CASE WHEN st.m['commit_weight'] = 'probability' THEN o.opp_probability / 100 ELSE CAST(st.m['commit_weight'] AS DOUBLE) END
       ELSE o.opp_probability / 100 END AS weight,
  o.opp_incremental_bookings_amount * (CASE WHEN o.opp_forecast_category = 'Commit'
       THEN CASE WHEN st.m['commit_weight'] = 'probability' THEN o.opp_probability / 100 ELSE CAST(st.m['commit_weight'] AS DOUBLE) END
       ELSE o.opp_probability / 100 END) AS forecast_amount,
  add_months(trunc(o.start_basis, 'MM'),
             (CASE WHEN day(o.start_basis) > 15 THEN 1 ELSE 0 END) + (CASE WHEN o.opp_forecast_category = 'Commit' THEN 0 ELSE 1 END)) AS accounting_start_month,
  CASE WHEN o.start_basis IS NULL THEN 'No contract effective or revenue start date - not spread'
       WHEN coalesce(o.opp_days_of_revenue, 0) <= 0 THEN 'No days of revenue - not spread'
       WHEN coalesce(o.opp_incremental_bookings_amount, 0) = 0 THEN 'No incremental amount'
       END AS issue
FROM o, st;

CREATE OR REPLACE VIEW {{schema}}.rev_pipeline_monthly
COMMENT 'rev_pipeline_opps spread by month: forecast_amount / min(days of revenue, 360) x 30.25 per month from the accounting start month while the deal runs; deals of 180+ days keep running after their end (renewal assumed). In budget_year_new_starts_only only deals starting that year count.'
AS
WITH st AS (SELECT map_from_entries(collect_list(struct(key, value))) AS m FROM {{schema}}.rev_settings),
months AS (
  SELECT explode(sequence(CAST(st.m['forecast_first_month'] AS DATE), CAST(st.m['forecast_last_month'] AS DATE), INTERVAL 1 MONTH)) AS month FROM st)
SELECT p.opp_code, p.opp_name, p.acc_name, p.team, p.team_leader, p.opp_forecast_category, p.opp_type,
       m.month,
       p.forecast_amount / least(p.opp_days_of_revenue, 360) * 30.25 AS revenue
FROM {{schema}}.rev_pipeline_opps p
JOIN months m
CROSS JOIN st
  ON p.issue IS NULL
 AND m.month >= p.accounting_start_month
 AND NOT (year(m.month) = CAST(nullif(st.m['budget_year_new_starts_only'], '') AS INT) AND year(p.accounting_start_month) <> year(m.month))
 AND (date_add(p.accounting_start_month, CAST(p.opp_days_of_revenue AS INT)) > date_sub(add_months(m.month, 1), 9)
      OR (p.opp_days_of_revenue > 179 AND date_add(p.opp_rev_start_date, CAST(p.opp_days_of_revenue AS INT)) > (SELECT min(month) FROM months)));
