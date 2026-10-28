-- Read-only login for a human dashboard. IAM authentication, no password.
-- SELECT on the FNOL schemas. No insert, update, delete, or DDL.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_dashboard') THEN
    CREATE ROLE fnol_dashboard LOGIN;
  END IF;
END
$$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO fnol_dashboard;
  END IF;
END
$$;

DO $$
DECLARE
  dbname text := current_database();
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO fnol_dashboard', dbname);
END
$$;

GRANT USAGE ON SCHEMA {{input}}, {{output}}, {{trace}}, {{mock}} TO fnol_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA {{input}} TO fnol_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA {{output}} TO fnol_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA {{trace}} TO fnol_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA {{mock}} TO fnol_dashboard;
