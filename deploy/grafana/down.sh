#!/usr/bin/env bash
# Stop local Grafana, the credential refresher and the Session Manager tunnel that ./up.sh started.
set -euo pipefail

cd "$(dirname "$0")"

FNOL_BI_PASSWORD=unused docker compose down

for pidfile in .creds.pid .tunnel.pid; do
  if [[ -f $pidfile ]]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
  fi
done
rm -f aws/credentials.json aws/credentials.json.tmp
