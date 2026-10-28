# AWS deploy

Account `104005345381`, region `us-east-1`. The VPC, NAT, ECS cluster, task roles, Cloud Map namespace, and EFS file system already exist. These templates attach PostgreSQL and new task revisions to that network. They do not create a VPC, and they do not modify the SQL Server instance named `fnol`.

Nothing in this repository deploys itself. The commands below are the order to run when you choose to.

## What is already there

```mermaid
flowchart TB
  ops["Operator via ECS Exec"]
  subgraph vpc["VPC vpc-05da07556a583ba88 10.43.0.0/16"]
    nat["NAT nat-0563fc6ab85404d39"]
    subgraph app["App subnets fnol-private-a and fnol-private-b"]
      analyst["analyst"]
      intake["intake :8080"]
      policy["policy :8080"]
      rules["rules :8080"]
      citations["citations :8080"]
      enterprise["enterprise :8767"]
      reviewer["reviewer :8090"]
      dns["Cloud Map fnol.internal"]
    end
    subgraph dbnets["DB subnets fnol-db-a and fnol-db-b"]
      pg["RDS PostgreSQL fnol-postgres"]
      legacy["SQL Server fnol legacy"]
    end
    efs["EFS fs-035b11da15aa05ed6 /mnt/state"]
  end
  ecr["ECR fnol"]
  s3["S3 fnol-104005345381-evidence and KMS"]
  bedrock["Bedrock Gemma"]
  logs["CloudWatch Logs /fnol"]
  ops --> analyst
  analyst --> dns
  dns --> intake
  dns --> policy
  dns --> rules
  dns --> citations
  intake --> enterprise
  policy --> enterprise
  rules --> enterprise
  intake --> pg
  policy --> pg
  rules --> pg
  citations --> pg
  analyst --> pg
  enterprise --> pg
  reviewer --> pg
  intake --> efs
  analyst --> efs
  enterprise --> efs
  reviewer --> efs
  app --> nat
  nat --> ecr
  nat --> s3
  nat --> bedrock
  nat --> logs
  intake --> s3
  citations --> s3
  rules --> bedrock
  analyst --> bedrock
```

Fargate cluster `fnol` runs seven services in `fnol-private-a` (`subnet-00eea3b5317c5e492`, 10.43.11.0/24) and `fnol-private-b` (`subnet-02cadb526cc74c182`, 10.43.12.0/24). `assignPublicIp` is disabled. Both subnets send their default route to NAT `nat-0563fc6ab85404d39`. There is no load balancer and there are no VPC endpoints.

Tasks share security group `fnol-tasks` (`sg-0902a6085d45388f5`). Its self-referencing ingress is 8080, 8767, and 8090. Egress today is TCP 8080, 8767, and 8090 to itself, TCP 443 to the internet, and TCP 2049 for EFS. There is no 5432 rule, so tasks cannot open a database connection until the data stack adds one. Task roles are `arn:aws:iam::104005345381:role/fnol/fnol-<service>`. The execution role is `role/fnol/fnol-execution`. Images come from `104005345381.dkr.ecr.us-east-1.amazonaws.com/fnol`. Logs are `/fnol/<service>`. The running image tag is `1.1.2` at 256 CPU and 512 MB. No ECS `secrets` are set.

Cloud Map private DNS namespace `fnol.internal` has `intake`, `policy`, `rules`, `citations`, `enterprise`, and `reviewer`. Analyst is not registered. Analyst runs `sleep infinity` so an operator can exec in, with `FNOL_STEP_TRANSPORT=http` and the step URLs pointed at Cloud Map.

Every step server listens on `FNOL_HTTP_PORT`. The process default is **8080**. In the VPC the four steps are `http://intake.fnol.internal:8080`, `http://policy.fnol.internal:8080`, `http://rules.fnol.internal:8080`, and `http://citations.fnol.internal:8080`. The client defaults of 8081–8084 exist only so four processes can bind on one laptop. ECS sets the Cloud Map URLs and does not use those ports. Enterprise stays on 8767. Reviewer stays on 8090.

State today is SQLite on EFS `fs-035b11da15aa05ed6` (`fnol-state`), encrypted, with transit encryption, mounted at `/mnt/state`. EFS is NFS. SQLite relies on POSIX advisory locks and byte-range locks, which NFS does not honor reliably across clients. Several tasks opening the same `input.sqlite`, `output.sqlite`, `trace.sqlite`, or `mock.sqlite` can stall on `database is locked`, lose a commit, or tear a page. WAL mode does not fix that. PostgreSQL is the replacement: one server, row locks, and a transaction advisory lock around the trace hash chain.

The DB subnets are `fnol-db-a` (`subnet-0c107464e812ac7cf`, 10.43.1.0/24) and `fnol-db-b` (`subnet-021865e74ab89de80`, 10.43.2.0/24). They already hold RDS instance `fnol`, engine `sqlserver-ex` 16.00, class `db.t3.micro`, security group `sg-0ab66856ba0288851`. That instance is the Azure-era SQL Server target. It is publicly accessible, encrypted, and has no IAM authentication and no managed master secret. This repository does not change it and will refuse `DbInstanceIdentifier=fnol`.

Foundation outputs already in use: bucket `fnol-104005345381-evidence`, KMS key `arn:aws:kms:us-east-1:104005345381:key/8f10cc48-a974-44fc-807a-60041558d8ab`, the ECR repository, and the role ARNs above.

## Stacks

1. `foundation.yaml` — already deployed. This change adds log group `/fnol/migrate` and lets the execution role write to it. That update does not replace running tasks.
2. `data.yaml` — DB subnet group `fnol-postgres` on the two db subnets, security group `fnol-postgres` allowing TCP 5432 only from `fnol-tasks`, one egress rule on `fnol-tasks` for TCP 5432 to that security group, and RDS PostgreSQL `fnol-postgres`. Private, KMS-encrypted, IAM auth on, managed master secret, deletion protection on, backups at least 7 days. `MultiAZ` defaults off. `CreateVpcEndpoints` defaults off because the NAT is already there. The egress rule is a standalone `AWS::EC2::SecurityGroupEgress`, so the existing 8080, 8767, 8090, 443, and 2049 rules stay. Deleting the data stack removes only that 5432 rule.
3. `services.yaml` — task definitions `fnol-<service>` plus `fnol-migrate`. `ManageServices` defaults to false, so the stack registers revisions and does not create or update the live services.

Live defaults are the template defaults and `deploy/aws/params/live-data.json`. Pass `PermissionsBoundaryArn` from the foundation `TaskBoundaryArn` output so `fnol-migrate` sits under the same ceiling as the task roles.

`EngineVersion` defaults to `16.8`. Confirm that minor is still offered before the first deploy.

## Deploy and cutover

Run these in order. Stop if a step fails. Do not roll the services until the migrate task exits 0.

1. Update the foundation stack. This adds `/fnol/migrate` and one log permission. Services keep their current tasks.

```sh
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name fnol-foundation \
  --template-file deploy/aws/foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

2. Deploy PostgreSQL. This does not replace instance `fnol`. The same deploy adds egress TCP 5432 on `fnol-tasks` (`sg-0902a6085d45388f5`) to security group `fnol-postgres` only. No separate security-group edit is required before the migrate task.

```sh
BOUNDARY=$(aws cloudformation describe-stacks --region us-east-1 \
  --stack-name fnol-foundation \
  --query "Stacks[0].Outputs[?OutputKey=='TaskBoundaryArn'].OutputValue" --output text)

aws cloudformation deploy \
  --region us-east-1 \
  --stack-name fnol-data \
  --template-file deploy/aws/data.yaml \
  --parameter-overrides file://deploy/aws/params/live-data.json \
    PermissionsBoundaryArn="$BOUNDARY" \
  --capabilities CAPABILITY_NAMED_IAM
```

3. Build a new image. Tag `1.1.2` was built without the `postgres` group and without `deploy/aws/sql`. A Postgres task definition rolled onto `1.1.2` will fail its health check and should roll back.

```sh
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin 104005345381.dkr.ecr.us-east-1.amazonaws.com
docker build -f deploy/Dockerfile -t 104005345381.dkr.ecr.us-east-1.amazonaws.com/fnol:NEW_TAG .
docker push 104005345381.dkr.ecr.us-east-1.amazonaws.com/fnol:NEW_TAG
```

4. Fill in `deploy/aws/params/live-services.json`. Set `DatabaseEndpoint` from the `fnol-data` output and `ImageTag` to `NEW_TAG`. Leave `StateBackend=postgres`, `MountEfs=true`, and `ManageServices=false`. The migrate task connects as the database role `fnol_migrate` with the IAM role `fnol-migrate`. `rds-db:connect` only signs that login in. It does not create tables. `008_migrate_role.sql` grants `fnol_migrate` `CREATE` on the database and on schema `public`. Service roles are not in that grant.

5. Register task definition revisions. With `ManageServices=false` the running services stay on `fnol-<service>:3`.

```sh
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name fnol-services \
  --template-file deploy/aws/services.yaml \
  --parameter-overrides file://deploy/aws/params/live-services.json \
  --capabilities CAPABILITY_NAMED_IAM
```

6. Run the migrate task once, from the app subnets, using `fnol-tasks`. It connects with IAM as `fnol_migrate` (`FNOL_RDS_IAM_AUTH=1`, `FNOL_RDS_HOST` set to the instance endpoint). It applies `deploy/aws/sql/postgres/*.sql` and records versions in `public.fnol_schema_migrations`. That creates schemas `input`, `output`, `trace`, and `mock`. Login roles are `fnol_intake`, `fnol_policy`, `fnol_rules`, `fnol_citations`, `fnol_analyst`, `fnol_enterprise`, `fnol_reviewer`, and `fnol_migrate`. On RDS those roles are granted `rds_iam`. Service tasks only check that the ledger already lists every file. They do not create `public.fnol_schema_migrations`.

`008_migrate_role.sql` creates `fnol_migrate` and grants it `CREATE` on schema `public` plus `INSERT` on the ledger. Service roles get `SELECT` only. The role cannot exist until that file runs, so apply it once as the RDS master user: override the migrate container with `FNOL_DB_SECRET_ARN` set to the managed secret. Leave `FNOL_RDS_HOST` as the database endpoint. The secret stores the username and password and has no host; `secret_host` uses `FNOL_RDS_HOST`. The `fnol-migrate` role may read that secret for this override. After `fnol_migrate` exists, run the task with no override so it signs in with IAM. Do not put the secret on the service tasks.

```sh
MIGRATE_TD=$(aws cloudformation describe-stacks --region us-east-1 \
  --stack-name fnol-services \
  --query "Stacks[0].Outputs[?OutputKey=='MigrateTaskDefinition'].OutputValue" --output text)

aws ecs run-task \
  --region us-east-1 \
  --cluster fnol \
  --launch-type FARGATE \
  --platform-version 1.4.0 \
  --task-definition "$MIGRATE_TD" \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-00eea3b5317c5e492,subnet-02cadb526cc74c182],securityGroups=[sg-0902a6085d45388f5],assignPublicIp=DISABLED}"
```

Wait until that task stops and its container exit code is 0. Read `/fnol/migrate` if it is not. Do not edit a SQL file that has already been applied. Add a new file and run the task again.

7. Roll each service onto its new revision. `minimumHealthyPercent=100` and `maximumPercent=200` start the new task before stopping the old one. The circuit breaker returns the service to the previous revision if the new task never becomes healthy. Revision 3 still mounts EFS and does not set `FNOL_RDS_HOST`, so a rollback keeps serving SQLite.

Roll enterprise first, then intake, policy, rules, citations, reviewer, and analyst last. Analyst's Postgres command imports `psycopg` and then `exec sleep infinity`, so a tag without the driver fails the task instead of looking healthy. Repeat the command with each service name and the matching stack output (`EnterpriseTaskDefinition`, `IntakeTaskDefinition`, and so on).

```sh
aws ecs update-service \
  --region us-east-1 \
  --cluster fnol \
  --service intake \
  --task-definition "$INTAKE_TD" \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-00eea3b5317c5e492,subnet-02cadb526cc74c182],securityGroups=[sg-0902a6085d45388f5],assignPublicIp=DISABLED}" \
  --deployment-configuration "minimumHealthyPercent=100,maximumPercent=200,deploymentCircuitBreaker={enable=true,rollback=true}" \
  --enable-execute-command
```

`update-service` does not drop the existing Cloud Map registration when the command omits `serviceRegistries`. Leave execute command enabled. Analyst is how operators enter the system.

8. On each new task, `GET /health` on 8080, 8767, or 8090. Then run one intake and confirm rows land in PostgreSQL. Do not load the old SQLite files. Evidence objects stay in S3 under `case-documents/` and `policy-wording/`.

9. Keep the EFS mount for the rollback window. After PostgreSQL has been the serving path and a rollback to SQLite is no longer wanted, set `MountEfs=false`, deploy `fnol-services` again, and roll the services the same way. Unmounting does not delete the file system.

Task environment when `StateBackend=postgres`:

```sh
FNOL_RDS_IAM_AUTH=1
FNOL_RDS_HOST=<DatabaseEndpoint>
FNOL_RDS_PORT=5432
FNOL_RDS_DB=fnol
FNOL_RDS_USER=fnol_<service>
FNOL_PG_NAMESPACE=shared
```

`FNOL_RDS_USER` is `fnol_intake`, `fnol_policy`, `fnol_rules`, `fnol_citations`, `fnol_analyst`, `fnol_enterprise`, or `fnol_reviewer`. The reviewer service connects as `fnol_reviewer` and serves the pending-cases page on port 8090. Leave `FNOL_DATABASE_URL` unset. The process builds a short-lived IAM token and connects with `sslmode=require`. With `FNOL_PG_NAMESPACE=shared`, `FNOL_STATE_DIR` is not used to choose a schema.

`FNOL_STATE_DIR` remains `/mnt/state` while the volume is mounted. Enterprise and reviewer keep `FNOL_MOCK_STATE=/mnt/state/mock.sqlite` for that window. Those paths are ignored for the connection once `FNOL_RDS_HOST` is set.

## Adopting the existing services

Preferred path: the CLI update in step 7. Registering a revision does not stop a task. The rolling deployment is the moment traffic moves, and the circuit breaker can reverse it.

Do not deploy `fnol-services` with `ManageServices=true` while the seven services already exist. CloudFormation would try to create them, fail with an existing-service error, and roll the stack back. The live services stay up. Task definition revisions created by that failed stack are removed with the rollback. The services continue on revision 3.

CloudFormation import is a later step, after the live services already match this template (same task definition, subnets, security group, desired count, and Cloud Map service ids). Import does not restart tasks by itself. The next template change that replaces the task definition does roll them, using the deployment settings in `services.yaml` (minimum 100, maximum 200, circuit breaker on). Resource identifiers are the cluster name and the service name. Set `DeletionPolicy: Retain` is already on the service resources, so deleting the stack later does not delete the service. Pass the real desired count. The template default is 1.

Importing today's revision 3 into this template will not succeed. The template describes the Postgres task definition, not the EFS-only revision that is running. Do not hand-edit the template back to revision 3 unless you are comparing it to the live task definition JSON field by field.

Lookup for a later import:

```sh
aws servicediscovery list-namespaces --region us-east-1
aws servicediscovery list-services --region us-east-1 \
  --filters Name=NAMESPACE_ID,Values=NAMESPACE_ID
```

Pass each `srv-` id as `IntakeDiscoveryServiceId` and the matching parameters. Analyst has no Cloud Map service. The template does not create a namespace or a discovery service, so it will not collide with `fnol.internal`.

## Legacy SQL Server

Leave instance `fnol` (`sqlserver-ex`) running. The application does not use it. Two changes are worth making, and both are owner decisions this template will not take:

- Set `PubliclyAccessible` to false. It is true today, on a database that has no IAM authentication.
- Delete the instance after the PostgreSQL cutover is accepted and nothing still connects to it.

## Local Postgres

```sh
docker compose up -d postgres
export FNOL_DATABASE_URL=postgresql://fnol:fnol@127.0.0.1:5432/fnol
export FNOL_PG_NAMESPACE=shared
uv run --group postgres python tools/migrate.py
```

`docker compose up mock` runs that migration and then the mock. Host commands without `FNOL_DATABASE_URL` or `FNOL_RDS_HOST` keep using SQLite files under `.state/`. To point the analyst at four local step servers, set `FNOL_STEP_TRANSPORT=http` and the `FNOL_*_URL` variables. If you do not set them, the client uses ports 8081–8084 on `127.0.0.1`.

CI sets `FNOL_PG_NAMESPACE=isolate` so each test directory gets its own schemas on one server. Do not use `isolate` for a real deployment.

## Owner decisions

- When to turn off public access on SQL Server `fnol`, and when to delete that instance.
- Whether PostgreSQL `MultiAZ` should be on. The default is off.
- Whether to add the optional VPC endpoints later. The default is off. The NAT already reaches ECR, Logs, Secrets Manager, KMS, S3, and Bedrock.
- How long to keep the EFS mount after PostgreSQL is serving, and whether to delete file system `fs-035b11da15aa05ed6` after that.
- Whether CloudFormation should import the ECS services after the rollout matches `services.yaml`. The default leaves the services under the CLI.
- The image tag to publish. `1.1.2` cannot run the Postgres command.
- The live desired count, if you import. The template default is 1.
