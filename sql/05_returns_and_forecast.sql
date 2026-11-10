-- Revenue forecasting demo: team returns views and the combined monthly revenue forecast.
-- rev_team_returns is (re)loaded by the notebook "Revenue Demo - Team Returns"; it is created empty here so the views exist.

CREATE TABLE IF NOT EXISTS {{schema}}.rev_team_returns (
  file_name STRING, region STRING, team STRING, cycle STRING, actual_months INT, forecast_year INT,
  sheet_name STRING, sheet_row INT, section STRING,
  parent_account STRING, account_name STRING, product_category STRING, product_name STRING, platform STRING, market STRING,
  effective_date DATE, end_date DATE, pricing_structure STRING, currency STRING, pid STRING, opportunity_id STRING,
  opportunity_name STRING, opportunity_stage STRING, review STRING, status STRING,
  team_leader STRING, director STRING, preparer STRING, product STRING,
  month DATE, amount DOUBLE, amount_text STRING, notes STRING, loaded_at TIMESTAMP)
COMMENT 'Team Team Volume Forecast returns, one row per workbook row and month. Loaded by notebook Revenue Demo - Team Returns.';

CREATE OR REPLACE VIEW {{schema}}.rev_team_forecast
COMMENT 'Latest returned cycle per team (10+2 beats 9+3), by PID, platform and month.'
AS
WITH latest AS (
  SELECT team, forecast_year, max(actual_months) AS actual_months
  FROM {{schema}}.rev_team_returns
  GROUP BY ALL
)
SELECT r.region, r.team, r.forecast_year, r.cycle, r.actual_months, r.file_name, r.parent_account, r.account_name, r.pid, r.platform,
       r.product_category, r.product, r.month, sum(coalesce(r.amount, 0)) AS amount,
       concat_ws(' | ', collect_set(r.notes)) AS notes
FROM {{schema}}.rev_team_returns r
JOIN latest l USING (team, forecast_year, actual_months)
GROUP BY ALL;

CREATE OR REPLACE VIEW {{schema}}.rev_team_forecast_pid
COMMENT 'Latest returned team forecast base (media spend / sales) by PID and month, all platforms added up – what the contract database looks up. first_forecast_month = first month after the cycle''s actuals.'
AS
SELECT pid, month, sum(amount) AS amount, collect_set(team) AS teams, collect_set(cycle) AS cycles, count(DISTINCT platform) AS platforms,
       min(make_date(forecast_year, 1, 1) + make_interval(0, actual_months)) AS first_forecast_month
FROM {{schema}}.rev_team_forecast
WHERE pid IS NOT NULL
GROUP BY ALL;

CREATE OR REPLACE VIEW {{schema}}.rev_forecast_monthly
COMMENT 'Monthly revenue forecast, one row per source line and month. Contract database lines: revenue from the contract database, except dynamic-rate lines in months the teams have forecast (from the cycle''s first forecast month), where revenue = team forecast base x rate, at least the monthly minimum. Plus the weighted open pipeline (rev_pipeline_monthly).'
AS
SELECT m.month, coalesce(c.team, 'Unassigned') AS team, c.salesforce_parent_company AS parent_account, c.salesforce_client AS client,
       c.contract_item_id AS pid, c.opportunity_id, c.product_category, c.rate_type,
       CASE WHEN s.pid IS NOT NULL THEN 'Team forecast' ELSE 'Contract database' END AS source,
       CASE WHEN s.pid IS NOT NULL THEN s.amount ELSE m.base_amount END AS base_amount,
       m.revenue AS contract_db_revenue,
       CASE WHEN s.pid IS NOT NULL THEN greatest(s.amount * c.commission_rate, coalesce(c.monthly_minimum, 0)) ELSE m.revenue END AS revenue
FROM {{schema}}.rev_contract_db c
JOIN {{schema}}.rev_contract_db_monthly m ON m.row_id = c.row_id
LEFT JOIN {{schema}}.rev_team_forecast_pid s
  ON c.rate_type = 'Dynamic' AND m.active_flag = 1 AND s.pid = c.contract_item_id AND s.month = m.month AND m.month >= s.first_forecast_month
UNION ALL
SELECT p.month, coalesce(p.team, 'Unassigned'), o.parent_account, p.acc_name, NULL, p.opp_code, NULL, NULL,
       concat('Pipeline – ', p.opp_forecast_category), NULL, NULL, p.revenue
FROM {{schema}}.rev_pipeline_monthly p
LEFT JOIN {{schema}}.src_sf_opportunity o ON o.opp_code = p.opp_code;
