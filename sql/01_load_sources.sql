-- Revenue forecasting demo: load the fake inputs (data/*.csv) uploaded to the volume {{volume}}/inputs/.
-- src_* tables stand in for the Salesforce data the live project reads from the data warehouse.
-- rev_contract_db / rev_contract_db_monthly stand in for the Excel contract database.

CREATE OR REPLACE FUNCTION {{schema}}.rev_num(s STRING) RETURNS DOUBLE
COMMENT 'Text to number: blank = null, accounting dash = 0, (x) = -x, thousands commas, x% = x/100.'
RETURN CASE
  WHEN nullif(trim(s), '') IS NULL THEN NULL
  WHEN trim(s) IN ('-', '$-') THEN 0D
  WHEN trim(s) RLIKE '^\\(.*\\)$' THEN -try_cast(regexp_replace(s, '[()$,%\\s]', '') AS DOUBLE) / (CASE WHEN s LIKE '%\\%%' THEN 100 ELSE 1 END)
  ELSE try_cast(regexp_replace(s, '[$,%\\s]', '') AS DOUBLE) / (CASE WHEN s LIKE '%\\%%' THEN 100 ELSE 1 END)
END;

CREATE OR REPLACE TABLE {{schema}}.src_sf_opportunity
COMMENT 'Demo stand-in for the Salesforce opportunity table: one row per deal (won, open pipeline, open renewals), with renewal links. Fake data.'
AS SELECT
  opp_id, opp_code, opp_name, opp_type, opp_stage, opp_forecast_category,
  CAST(opp_probability AS DOUBLE) AS opp_probability,
  CAST(opp_close_date AS DATE) AS opp_close_date, CAST(opp_effective_date AS DATE) AS opp_effective_date,
  CAST(opp_rev_start_date AS DATE) AS opp_rev_start_date, CAST(opp_contract_end_date AS DATE) AS opp_contract_end_date,
  CAST(opp_days_of_revenue AS INT) AS opp_days_of_revenue,
  CAST(opp_deal_amount AS DOUBLE) AS opp_deal_amount, CAST(opp_incremental_bookings_amount AS DOUBLE) AS opp_incremental_bookings_amount,
  opp_currency, acc_name, parent_account, nullif(team, '') AS team, team_leader,
  opp_owner_name, opp_owner_team, opp_team_bucket,
  nullif(opp_previous_opp_code, '') AS opp_previous_opp_code, nullif(opp_next_opp_code, '') AS opp_next_opp_code,
  nullif(opp_next_opp_stage, '') AS opp_next_opp_stage, nullif(opp_next_opp_id, '') AS opp_next_opp_id, opp_link,
  current_timestamp() AS refreshed_at
FROM read_files('{{volume}}/inputs/sf_opportunity.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REPLACE TABLE {{schema}}.src_sf_opportunity_products
COMMENT 'Demo stand-in for Salesforce opportunity products with platforms: one row per product (PID) x platform x market. Fake data.'
AS SELECT
  product_id, platform, nullif(market, '') AS market, pricing_structure,
  product_category, product_name, opp_id, opp_code, opp_name, opp_type, opp_stage,
  CAST(opp_probability AS DOUBLE) AS opp_probability, opp_forecast_category,
  CAST(opp_close_date AS DATE) AS opp_close_date, CAST(opp_effective_date AS DATE) AS opp_effective_date,
  CAST(opp_contract_end_date AS DATE) AS opp_contract_end_date, opp_currency,
  CAST(opp_deal_amount AS DOUBLE) AS opp_deal_amount, CAST(product_amount AS DOUBLE) AS product_amount,
  acc_name, parent_account, nullif(team, '') AS team, opp_owner_name, opp_owner_team,
  nullif(opp_next_opp_id, '') AS opp_next_opp_id, opp_link,
  current_timestamp() AS refreshed_at
FROM read_files('{{volume}}/inputs/sf_opportunity_products.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REPLACE TABLE {{schema}}.src_teams
COMMENT 'Demo stand-in for the teams dimension: team code, leader, region, email (example.com). Fake data.'
AS SELECT team, team_leader, region, nullif(to_emails, '') AS to_emails
FROM read_files('{{volume}}/inputs/teams.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REPLACE TABLE {{schema}}.rev_contract_db
COMMENT 'Contract database, one row per contract line (PID): won deals and open auto-renewals with rate type, rates, dates and team. row_id = sheet row. Fake data.'
AS SELECT
  CAST(row_id AS INT) AS row_id, contract_item_id, opportunity_id, opportunity_stage, opportunity_name, revenue_type, client_type,
  salesforce_parent_company, salesforce_client, product, product_category, media_platform, geo, team, country,
  rate_type, nullif(dynamic_rate_source, '') AS dynamic_rate_source, pricing_structure,
  {{schema}}.rev_num(commission_rate) AS commission_rate,
  {{schema}}.rev_num(flat_rate) AS flat_rate, {{schema}}.rev_num(monthly_minimum) AS monthly_minimum, billing_currency,
  CAST(nullif(sf_start_date, '') AS DATE) AS sf_start_date, CAST(nullif(sf_end_date, '') AS DATE) AS sf_end_date,
  CAST(nullif(forecast_start_date, '') AS DATE) AS forecast_start_date,
  CAST(nullif(forecast_termination_date, '') AS DATE) AS forecast_termination_date,
  CAST(contract_length AS INT) AS contract_length,
  nullif(previous_opportunity_opp_id, '') AS previous_opportunity_opp_id, nullif(next_renewal_opp_id, '') AS next_renewal_opp_id,
  renewal_assumption, 'contract_db.csv' AS source_file, current_timestamp() AS loaded_at
FROM read_files('{{volume}}/inputs/contract_db.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REPLACE TABLE {{schema}}.rev_contract_db_monthly
COMMENT 'Contract database monthly blocks, one row per contract line and month Jan-25..Dec-27: base_amount = media spend / sales the rate applies to, active_flag, revenue. Fake data.'
AS SELECT CAST(row_id AS INT) AS row_id, contract_item_id, CAST(month AS DATE) AS month,
          CAST(base_amount AS DOUBLE) AS base_amount, CAST(active_flag AS DOUBLE) AS active_flag, CAST(revenue AS DOUBLE) AS revenue
FROM read_files('{{volume}}/inputs/contract_db_monthly.csv', format => 'csv', header => true, inferColumnTypes => false);
