"""Static checks for the RDS template. No AWS account required."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = (ROOT / "deploy" / "aws" / "data.yaml").read_text()
FOUNDATION = (ROOT / "deploy" / "aws" / "foundation.yaml").read_text()
SERVICES = (ROOT / "deploy" / "aws" / "services.yaml").read_text()
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text()


class DataTemplateTests(unittest.TestCase):
    def test_database_is_private_encrypted_and_backed_up(self):
        self.assertIn("PubliclyAccessible: false", DATA)
        self.assertIn("StorageEncrypted: true", DATA)
        self.assertIn("ManageMasterUserPassword: true", DATA)
        self.assertIn("EnableIAMDatabaseAuthentication: true", DATA)
        self.assertIn("BackupRetentionPeriod:", DATA)
        self.assertIn("MinValue: 7", DATA)
        self.assertNotIn("0.0.0.0/0", DATA)
        self.assertNotRegex(DATA, r"(?m)^\s*MasterUserPassword:")

    def test_multi_az_is_a_parameter(self):
        self.assertIn("MultiAZ:", DATA)
        self.assertIn('Default: "false"', DATA)

    def test_foundation_key_allows_rds_and_secrets(self):
        self.assertIn("AllowRdsInAccount", FOUNDATION)
        self.assertIn("AllowSecretsManager", FOUNDATION)
        self.assertIn("deploy/aws/data.yaml", FOUNDATION)
        self.assertIn("LogGroupName: /fnol/migrate", FOUNDATION)

    def test_reuses_the_live_network_and_leaves_sql_server_alone(self):
        self.assertNotRegex(DATA, r"Type: AWS::EC2::VPC\n")
        self.assertNotIn("Type: AWS::EC2::Subnet", DATA)
        self.assertNotIn("sqlserver", DATA)
        self.assertIn("Default: vpc-05da07556a583ba88", DATA)
        self.assertIn("Default: subnet-0c107464e812ac7cf", DATA)
        self.assertIn("Default: subnet-021865e74ab89de80", DATA)
        self.assertIn("Default: sg-0902a6085d45388f5", DATA)
        self.assertIn("Default: fnol-postgres", DATA)
        self.assertIn("LeaveTheSqlServerAlone", DATA)
        self.assertIn('Default: "false"', DATA)
        self.assertIn("FromPort: 5432", DATA)
        self.assertIn("SourceSecurityGroupId: !Ref TaskSecurityGroupId", DATA)
        self.assertIn("Type: AWS::EC2::SecurityGroupEgress", DATA)
        self.assertIn("GroupId: !Ref TaskSecurityGroupId", DATA)
        self.assertIn("DestinationSecurityGroupId: !Ref DatabaseSecurityGroup", DATA)

    def test_services_match_the_live_tasks_and_add_rds_users(self):
        for family in (
            "fnol-intake",
            "fnol-policy",
            "fnol-rules",
            "fnol-citations",
            "fnol-analyst",
            "fnol-enterprise",
            "fnol-reviewer",
            "fnol-migrate",
        ):
            self.assertIn(f"Family: {family}", SERVICES)
        for user in (
            "fnol_intake",
            "fnol_policy",
            "fnol_rules",
            "fnol_citations",
            "fnol_analyst",
            "fnol_enterprise",
            "fnol_reviewer",
        ):
            self.assertIn(f"Value: {user}", SERVICES)
        self.assertIn('Value: "8080"', SERVICES)
        self.assertNotIn("8081", SERVICES)
        self.assertNotIn("8082", SERVICES)
        self.assertIn("http://intake.${DnsNamespace}:8080", SERVICES)
        self.assertIn("FNOL_RDS_IAM_AUTH", SERVICES)
        self.assertIn("FNOL_PG_NAMESPACE", SERVICES)
        self.assertIn('Default: "false"', SERVICES)
        self.assertIn("AssignPublicIp: DISABLED", SERVICES)
        self.assertIn("fs-035b11da15aa05ed6", SERVICES)
        self.assertIn("server/citation_server.py", SERVICES)
        self.assertIn("tools/migrate.py", SERVICES)
        self.assertIn("Value: fnol_migrate", SERVICES)
        self.assertNotIn("FNOL_DB_SECRET_ARN", SERVICES)
        self.assertNotIn("MigrateSecretArn", SERVICES)
        self.assertIn("dbuser:${DbResource}/fnol_migrate", DATA)
        self.assertIn("secretsmanager:GetSecretValue", DATA)
        self.assertIn("Name: fnol-shadow-summary", FOUNDATION)
        self.assertIn("Type: AWS::Bedrock::Guardrail", FOUNDATION)
        self.assertNotIn("guardrail/*", FOUNDATION)
        self.assertEqual(FOUNDATION.count("bedrock:ApplyGuardrail"), 1)
        self.assertEqual(SERVICES.count("BEDROCK_GUARDRAIL_ID"), 1)
        self.assertIn("!ImportValue fnol-shadow-guardrail-id", SERVICES)
        grant = (ROOT / "deploy" / "aws" / "sql" / "postgres" / "008_migrate_role.sql").read_text()
        self.assertIn("CREATE ROLE fnol_migrate LOGIN", grant)
        self.assertIn("GRANT CREATE ON SCHEMA public TO fnol_migrate", grant)
        self.assertIn("GRANT SELECT, INSERT ON public.fnol_schema_migrations TO fnol_migrate", grant)
        self.assertIn("GRANT SELECT ON public.fnol_schema_migrations", grant)
        self.assertNotIn("GRANT CREATE ON SCHEMA public TO fnol_enterprise", grant)
        self.assertNotRegex(SERVICES, r"Type: AWS::EC2::VPC\n")
        self.assertNotIn("sqlserver", SERVICES)

    def test_image_contains_the_driver_and_the_sql(self):
        self.assertIn("--group postgres", DOCKERFILE)
        self.assertIn("COPY deploy/aws/sql ./deploy/aws/sql", DOCKERFILE)


if __name__ == "__main__":
    unittest.main()
