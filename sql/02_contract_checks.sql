-- Revenue forecasting demo: Salesforce vs contract database checks.
-- Replaces the weekly manual check of a Salesforce export against the contract database.

CREATE OR REPLACE VIEW {{schema}}.rev_sf_pid
COMMENT 'Salesforce, one row per PID (opportunity product id): deal, account and product attributes plus renewal links. Products come one row per platform/market, so collapsed here.'
AS
SELECT
  o.product_id AS pid,
  max(o.opp_code) AS opp_code, max(o.opp_name) AS opp_name, max(o.opp_type) AS opp_type, max(o.opp_stage) AS opp_stage,
  max(o.opp_probability) AS opp_probability, max(o.opp_forecast_category) AS opp_forecast_category,
  max(o.opp_close_date) AS opp_close_date, max(o.opp_effective_date) AS opp_effective_date,
  max(o.opp_contract_end_date) AS opp_contract_end_date,
  max(o.acc_name) AS acc_name, max(o.parent_account) AS acc_parent, max(o.team) AS team,
  max(o.product_category) AS product_category, max(o.product_name) AS product_name, max(o.pricing_structure) AS pricing_structure,
  max(o.opp_currency) AS opp_currency, max(o.opp_deal_amount) AS opp_deal_amount,
  max(o.product_amount) AS pid_amount,
  max(o.opp_owner_name) AS opp_owner, max(o.opp_owner_team) AS opp_owner_team,
  max(d.opp_previous_opp_code) AS previous_opp_code, max(d.opp_next_opp_code) AS next_opp_code,
  max(d.opp_next_opp_stage) AS next_opp_stage, max(o.opp_link) AS opp_link, max(o.refreshed_at) AS source_refreshed_at
FROM {{schema}}.src_sf_opportunity_products o
LEFT JOIN {{schema}}.src_sf_opportunity d ON d.opp_id = o.opp_id
WHERE o.product_id IS NOT NULL
GROUP BY o.product_id;

-- Which Salesforce product categories belong in the contract database. Seeded once from what the contract database
-- holds; edit in_contract_db to change scope. A category not listed shows up in the new-PID check as
-- "Category not mapped" so it is never silently skipped.
CREATE TABLE IF NOT EXISTS {{schema}}.rev_map_category_scope (
  product_category STRING NOT NULL,
  in_contract_db BOOLEAN NOT NULL,
  note STRING,
  updated_at TIMESTAMP)
COMMENT 'Salesforce product category -> whether its PIDs belong in the contract database. User-editable.';

MERGE INTO {{schema}}.rev_map_category_scope t
USING (
  SELECT s.product_category,
         max(c.contract_item_id IS NOT NULL) AS in_contract_db,
         CASE WHEN max(c.contract_item_id IS NOT NULL) THEN 'Seeded: PIDs of this category are in the contract database'
              ELSE 'Seeded: no PID of this category is in the contract database' END AS note
  FROM {{schema}}.rev_sf_pid s
  LEFT JOIN (SELECT DISTINCT contract_item_id FROM {{schema}}.rev_contract_db) c ON c.contract_item_id = s.pid
  WHERE s.product_category IS NOT NULL AND s.opp_stage IN ('Closed Won') AND s.opp_contract_end_date >= DATE'2025-01-01'
  GROUP BY s.product_category) s
ON t.product_category = s.product_category
WHEN NOT MATCHED THEN INSERT (product_category, in_contract_db, note, updated_at) VALUES (s.product_category, s.in_contract_db, s.note, current_timestamp());

CREATE OR REPLACE VIEW {{schema}}.rev_check_new_pids
COMMENT 'PIDs to add to the contract database: won (Closed Won) PIDs and open auto-renewals in in-scope categories, contract ending 2025 or later, whose PID is not in it yet. Open new deals are not here: they belong to the pipeline forecast.'
AS
SELECT
  CASE WHEN m.product_category IS NULL THEN 'Category not mapped - check scope'
       WHEN s.opp_stage IN ('Closed Won') THEN 'Won - add to contract database'
       ELSE 'Open renewal - add to contract database' END AS action,
  s.pid, s.opp_code, s.opp_name, s.opp_type, s.opp_stage, s.opp_close_date, s.opp_effective_date, s.opp_contract_end_date,
  s.acc_name, s.acc_parent, s.team, s.product_category, s.product_name, s.pricing_structure, s.opp_currency, s.pid_amount,
  s.previous_opp_code, s.opp_owner, s.opp_link
FROM {{schema}}.rev_sf_pid s
LEFT JOIN {{schema}}.rev_map_category_scope m ON m.product_category = s.product_category
WHERE coalesce(m.in_contract_db, true)
  AND coalesce(s.opp_contract_end_date, DATE'2099-12-31') >= DATE'2025-01-01'
  AND (s.opp_stage IN ('Closed Won')
       OR (s.opp_stage NOT IN ('Closed Lost') AND s.opp_type ILIKE '%renewal%' AND s.previous_opp_code IS NOT NULL))
  AND NOT EXISTS (SELECT 1 FROM {{schema}}.rev_contract_db c WHERE c.contract_item_id = s.pid);

CREATE OR REPLACE VIEW {{schema}}.rev_check_salesforce
COMMENT 'Contract database vs Salesforce, one row per PID and field that disagrees: PID missing in Salesforce, stage, opportunity id, start date, end date, team, next renewal opp. Unassigned team = blank and 2099-12-31 end date = no end date count as equal.'
AS
WITH c AS (
  SELECT contract_item_id AS pid,
         min(row_id) AS first_row, count(*) AS file_rows,
         max(opportunity_stage) AS stage, max(opportunity_id) AS opp_code, max(sf_start_date) AS start_date,
         max(sf_end_date) AS end_date, max(team) AS team, max(next_renewal_opp_id) AS next_opp_code,
         max(salesforce_client) AS client, max(product) AS product
  FROM {{schema}}.rev_contract_db
  WHERE contract_item_id LIKE 'PID-%'
  GROUP BY contract_item_id),
cmp AS (
  SELECT c.*, s.pid AS sf_pid, s.opp_stage, s.opp_code AS sf_opp_code, s.opp_effective_date, s.opp_contract_end_date,
         s.team AS sf_team, s.next_opp_code AS sf_next_opp_code, s.opp_link
  FROM c LEFT JOIN {{schema}}.rev_sf_pid s ON s.pid = c.pid)
SELECT pid, first_row, file_rows, client, product, field, file_value, salesforce_value, opp_link
FROM cmp
LATERAL VIEW stack(7,
  'PID not in Salesforce', 'present', CAST(NULL AS STRING), sf_pid IS NULL,
  'Stage', stage, opp_stage, sf_pid IS NOT NULL AND NOT (trim(stage) <=> opp_stage),
  'Opportunity ID', opp_code, sf_opp_code, sf_pid IS NOT NULL AND NOT (trim(opp_code) <=> sf_opp_code),
  'SF start date', CAST(start_date AS STRING), CAST(opp_effective_date AS STRING), sf_pid IS NOT NULL AND NOT (start_date <=> opp_effective_date),
  'SF end date', CAST(end_date AS STRING), CAST(opp_contract_end_date AS STRING), sf_pid IS NOT NULL AND NOT (nullif(end_date, DATE'2099-12-31') <=> opp_contract_end_date),
  'Team', team, sf_team, sf_pid IS NOT NULL AND NOT (nullif(trim(team), 'Unassigned') <=> nullif(sf_team, 'Unassigned')),
  'Next renewal opp', next_opp_code, sf_next_opp_code, sf_pid IS NOT NULL AND sf_next_opp_code IS NOT NULL AND NOT (trim(next_opp_code) <=> sf_next_opp_code)
) x AS field, file_value, salesforce_value, differs
WHERE differs;
