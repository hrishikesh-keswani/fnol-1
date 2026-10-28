-- The migrate task signs in as fnol_migrate with IAM (rds-db:connect).
-- That action only allows the login. It does not grant CREATE TABLE.
-- On RDS for PostgreSQL 15+, public has no CREATE for ordinary roles, which
-- is why a service login fails with "permission denied for schema public".
-- fnol_migrate receives the database privileges that create and record
-- objects. Service roles may read the ledger and nothing else in public.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fnol_migrate') THEN
    CREATE ROLE fnol_migrate LOGIN;
  END IF;
END
$$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO fnol_migrate;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_superuser') THEN
    GRANT rds_superuser TO fnol_migrate;
  END IF;
END
$$;

DO $$
DECLARE
  dbname text := current_database();
BEGIN
  EXECUTE format('GRANT CONNECT, CREATE ON DATABASE %I TO fnol_migrate', dbname);
END
$$;

-- rds_superuser is not a superuser, and RDS revokes CREATE on public from
-- PUBLIC. This grant is what lets the migrate role create the ledger and
-- later objects. Do not give it to the service roles.
GRANT CREATE ON SCHEMA public TO fnol_migrate;

GRANT SELECT, INSERT ON public.fnol_schema_migrations TO fnol_migrate;

GRANT SELECT ON public.fnol_schema_migrations TO
  fnol_intake,
  fnol_policy,
  fnol_rules,
  fnol_citations,
  fnol_analyst,
  fnol_enterprise,
  fnol_reviewer;
