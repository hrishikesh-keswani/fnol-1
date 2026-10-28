#!/usr/bin/env bash
# Start local Grafana on http://localhost:3000 with the FNOL dashboards.
#
# Needs the AWS CLI with an SSO profile (default: fnol), session-manager-plugin,
# and Docker. Opens a Session Manager tunnel to fnol-postgres on 127.0.0.1:15432
# through the fnol-dashboard-tunnel instance, reads the fnol_bi password from
# Secrets Manager, and keeps short-lived CloudWatch credentials refreshed.
# Re-run when the SSO session expires; ./down.sh stops everything it started.
set -euo pipefail

cd "$(dirname "$0")"

export AWS_PROFILE="${AWS_PROFILE:-fnol}"
export AWS_REGION=us-east-1
LOCAL_PORT=15432
BI_SECRET=fnol/bi-readonly

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  aws sso login
fi

if ! nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null; then
  tunnel_id=$(aws ec2 describe-instances \
    --filters Name=tag:Name,Values=fnol-dashboard-tunnel Name=instance-state-name,Values=running \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  db_host=$(aws rds describe-db-instances --db-instance-identifier fnol-postgres \
    --query 'DBInstances[0].Endpoint.Address' --output text)
  if [[ -z "$tunnel_id" || "$tunnel_id" == "None" ]]; then
    echo "fnol-dashboard-tunnel instance is not running" >&2
    exit 1
  fi
  nohup aws ssm start-session --target "$tunnel_id" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"$db_host\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
    >.tunnel.log 2>&1 &
  echo $! >.tunnel.pid
  for _ in $(seq 1 30); do
    nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null && break
    sleep 1
  done
  if ! nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null; then
    echo "tunnel did not open; see deploy/grafana/.tunnel.log" >&2
    exit 1
  fi
fi

FNOL_BI_PASSWORD=$(aws secretsmanager get-secret-value --secret-id "$BI_SECRET" \
  --query SecretString --output text | python3 -c 'import json, sys; print(json.load(sys.stdin)["password"])')
export FNOL_BI_PASSWORD

# CloudWatch credentials last one hour; rewrite them every 5 minutes while the SSO session lasts.
refresh_creds() {
  (umask 077 && aws configure export-credentials --format process >aws/credentials.json.tmp) &&
    mv aws/credentials.json.tmp aws/credentials.json
}
[[ -f .creds.pid ]] && kill "$(cat .creds.pid)" 2>/dev/null || true
refresh_creds
export -f refresh_creds
nohup bash -c 'while sleep 300; do refresh_creds || echo "$(date) credential refresh failed; re-run ./up.sh"; done' \
  >.creds.log 2>&1 &
echo $! >.creds.pid

docker compose up -d --force-recreate

echo "Grafana: http://localhost:3000  (first login admin / admin)"
