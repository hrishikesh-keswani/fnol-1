-- Least-privilege roles matching the foundation task roles.
-- rds_iam is granted only when this database is Amazon RDS.
-- Local Postgres skips that grant; tests connect as the database owner.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_intake') THEN
    CREATE ROLE fnol_intake LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_policy') THEN
    CREATE ROLE fnol_policy LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_rules') THEN
    CREATE ROLE fnol_rules LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_citations') THEN
    CREATE ROLE fnol_citations LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_analyst') THEN
    CREATE ROLE fnol_analyst LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_enterprise') THEN
    CREATE ROLE fnol_enterprise LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_reviewer') THEN
    CREATE ROLE fnol_reviewer LOGIN;
  END IF;
END
$$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO fnol_intake;
    GRANT rds_iam TO fnol_policy;
    GRANT rds_iam TO fnol_rules;
    GRANT rds_iam TO fnol_citations;
    GRANT rds_iam TO fnol_analyst;
    GRANT rds_iam TO fnol_enterprise;
    GRANT rds_iam TO fnol_reviewer;
  END IF;
END
$$;

DO $$
DECLARE
  dbname text := current_database();
  role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY[
    'fnol_intake', 'fnol_policy', 'fnol_rules', 'fnol_citations',
    'fnol_analyst', 'fnol_enterprise', 'fnol_reviewer'
  ]
  LOOP
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', dbname, role_name);
  END LOOP;
END
$$;

GRANT USAGE ON SCHEMA {{input}} TO fnol_intake, fnol_policy, fnol_rules, fnol_citations, fnol_analyst;
GRANT USAGE ON SCHEMA {{output}} TO fnol_analyst, fnol_reviewer;
GRANT USAGE ON SCHEMA {{trace}} TO fnol_intake, fnol_policy, fnol_rules, fnol_citations, fnol_analyst;
GRANT USAGE ON SCHEMA {{mock}} TO fnol_enterprise, fnol_reviewer;

GRANT SELECT, INSERT ON {{input}}.input_revisions TO fnol_intake;
GRANT SELECT ON {{input}}.input_revisions TO fnol_policy, fnol_rules, fnol_citations, fnol_analyst;

GRANT SELECT, INSERT, DELETE ON {{input}}.procedure_sources TO fnol_intake;
GRANT SELECT ON {{input}}.procedure_sources TO fnol_policy, fnol_rules, fnol_citations, fnol_analyst;

GRANT SELECT, INSERT ON {{input}}.tool_payloads TO fnol_policy, fnol_rules;
GRANT SELECT ON {{input}}.tool_payloads TO fnol_intake, fnol_citations, fnol_analyst;

GRANT SELECT, INSERT ON {{input}}.decisions TO fnol_policy, fnol_rules, fnol_citations;
GRANT SELECT ON {{input}}.decisions TO fnol_intake, fnol_analyst;

GRANT SELECT, INSERT, UPDATE ON {{output}}.drafts TO fnol_analyst;
GRANT SELECT ON {{output}}.drafts TO fnol_reviewer;

GRANT SELECT, INSERT ON {{trace}}.trace_events TO fnol_intake, fnol_policy, fnol_rules, fnol_citations, fnol_analyst;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  {{mock}}.reviews, {{mock}}.records, {{mock}}.keys, {{mock}}.calls, {{mock}}.audit
  TO fnol_enterprise;
GRANT SELECT, UPDATE ON {{mock}}.reviews TO fnol_reviewer;
GRANT SELECT, INSERT ON {{mock}}.audit TO fnol_reviewer;
GRANT SELECT ON {{mock}}.records, {{mock}}.keys, {{mock}}.calls TO fnol_reviewer;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {{input}} TO fnol_intake, fnol_policy, fnol_rules, fnol_citations;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {{output}} TO fnol_analyst;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {{trace}} TO fnol_intake, fnol_policy, fnol_rules, fnol_citations, fnol_analyst;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {{mock}} TO fnol_enterprise, fnol_reviewer;
