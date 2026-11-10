-- Revenue forecasting demo: schema and volumes. Run first, then upload data/*.csv to {{volume}}/inputs/.
CREATE SCHEMA IF NOT EXISTS {{schema}} COMMENT 'Revenue forecasting demo – fake data only';

CREATE VOLUME IF NOT EXISTS {{schema}}.inputs COMMENT 'Revenue demo: fake input CSVs (data/ folder of the project)';

CREATE VOLUME IF NOT EXISTS {{schema}}.revenue_teams
COMMENT 'Revenue demo: sends/<cycle>/ workbooks built for the teams, returns/<year>/<team>/ the filled-in workbooks they send back.';
