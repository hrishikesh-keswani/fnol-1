-- Append-only intake revisions, tool snapshots, and decisions.
-- procedure_sources is replaced on each intake run and stays mutable.

CREATE TABLE IF NOT EXISTS {{input}}.input_revisions (
  case_id text NOT NULL,
  revision integer NOT NULL,
  ingested_at text NOT NULL,
  document_set_sha256 text NOT NULL,
  trace_id text,
  intake_source text,
  account_text text,
  incident_timestamp text,
  policy_number text,
  vehicle_id text,
  loss_type text,
  driver_name text,
  driver_name_supplied smallint,
  incident_location text,
  incident_location_supplied smallint,
  injury_reported_raw text,
  injury_reported smallint,
  vehicle_drivable_raw text,
  vehicle_drivable smallint,
  referral_indicator text,
  loss_reference text,
  intake_sha256 text,
  raw_intake text,
  intake_notes text,
  estimate_source_id text,
  estimate_sha256 text,
  estimate_reference text,
  estimate_vehicle text,
  repair_amount text,
  estimate_prepared_by text,
  estimate_notes text,
  raw_estimate text,
  report_source_id text,
  report_sha256 text,
  report_vehicle_reference text,
  report_incident_timestamp text,
  reported_loss_type text,
  report_notes text,
  raw_report text,
  repeated_fields text,
  extra_fields text,
  injection_field_status text,
  injection_model_status text,
  PRIMARY KEY (case_id, revision)
);

CREATE TABLE IF NOT EXISTS {{input}}.procedure_sources (
  source_id text PRIMARY KEY,
  sha256 text NOT NULL,
  loaded_at text NOT NULL
);

CREATE TABLE IF NOT EXISTS {{input}}.tool_payloads (
  case_id text NOT NULL,
  input_revision integer NOT NULL,
  tool text NOT NULL,
  raw_json text NOT NULL,
  sha256 text NOT NULL,
  stored_at text NOT NULL,
  PRIMARY KEY (case_id, input_revision, tool),
  CONSTRAINT tool_payloads_tool_known CHECK (tool IN ('policy', 'claims'))
);

CREATE TABLE IF NOT EXISTS {{input}}.decisions (
  case_id text NOT NULL,
  input_revision integer NOT NULL,
  kind text NOT NULL,
  body_json text NOT NULL,
  policy_sha256 text,
  claims_sha256 text,
  stored_at text NOT NULL,
  PRIMARY KEY (case_id, input_revision, kind),
  CONSTRAINT decisions_kind_known CHECK (kind IN ('policy', 'route', 'citations'))
);

CREATE OR REPLACE FUNCTION {{input}}.reject_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS input_revisions_no_update ON {{input}}.input_revisions;
CREATE TRIGGER input_revisions_no_update
BEFORE UPDATE ON {{input}}.input_revisions
FOR EACH ROW EXECUTE FUNCTION {{input}}.reject_append_only();

DROP TRIGGER IF EXISTS input_revisions_no_delete ON {{input}}.input_revisions;
CREATE TRIGGER input_revisions_no_delete
BEFORE DELETE ON {{input}}.input_revisions
FOR EACH ROW EXECUTE FUNCTION {{input}}.reject_append_only();

DROP TRIGGER IF EXISTS tool_payloads_no_update ON {{input}}.tool_payloads;
CREATE TRIGGER tool_payloads_no_update
BEFORE UPDATE ON {{input}}.tool_payloads
FOR EACH ROW EXECUTE FUNCTION {{input}}.reject_append_only();

DROP TRIGGER IF EXISTS tool_payloads_no_delete ON {{input}}.tool_payloads;
CREATE TRIGGER tool_payloads_no_delete
BEFORE DELETE ON {{input}}.tool_payloads
FOR EACH ROW EXECUTE FUNCTION {{input}}.reject_append_only();

DROP TRIGGER IF EXISTS decisions_no_update ON {{input}}.decisions;
CREATE TRIGGER decisions_no_update
BEFORE UPDATE ON {{input}}.decisions
FOR EACH ROW EXECUTE FUNCTION {{input}}.reject_append_only();

DROP TRIGGER IF EXISTS decisions_no_delete ON {{input}}.decisions;
CREATE TRIGGER decisions_no_delete
BEFORE DELETE ON {{input}}.decisions
FOR EACH ROW EXECUTE FUNCTION {{input}}.reject_append_only();
