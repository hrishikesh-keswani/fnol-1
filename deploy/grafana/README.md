# Grafana dashboards

Local Grafana with two dashboards over the live FNOL data in `fnol-postgres` and CloudWatch:

- **FNOL Executive Summary** (`fnol-executive`, home page): claims received, then a breakdown that adds up to that total (processing, pending, duplicate, approved but not processed, submitted, rejected, needs attention), gold-check accuracy, claim journey, processing time, AI cost split into prompt injection, summary, and total, step success rate, risks caught and claims waiting on a person.
- **FNOL Operations & AI Performance** (`fnol-ops`): per-claim register with prompt-injection and summary tokens and cost, decisions by reviewer, gold-check failures by check and by claim, Gemma token and cost breakdown, step latency, errors and AWS health.

Headline numbers appear only on the executive summary. The operations dashboard holds the drill-down behind them, so the two never repeat a panel.

## Run

Needs Docker, AWS CLI v2, the Session Manager plugin, and the `fnol` SSO profile (account `104005345381`, role `AWSAdministratorAccess`, region `us-east-1`).

```sh
deploy/grafana/up.sh     # http://localhost:3000, first login admin / admin
deploy/grafana/down.sh   # stops Grafana, the tunnel and the credential refresher
```

`up.sh` signs in with SSO if needed, opens a Session Manager tunnel through `fnol-dashboard-tunnel` to `fnol-postgres` on `127.0.0.1:15432`, reads the `fnol_bi` password from Secrets Manager (`fnol/bi-readonly`), and starts Grafana. CloudWatch credentials last one hour, so `up.sh` also starts a background job that rewrites `aws/credentials.json` every 5 minutes; Grafana reads it through `credential_process` in `aws/config`. Re-run `up.sh` when the SSO session expires.

Panels refresh every minute. Grafana listens on `127.0.0.1` only.

## Database login

Grafana connects as `fnol_bi`: a password login with `SELECT` on `input`, `output`, `trace` and `mock`, `default_transaction_read_only = on`, a 60 second statement timeout and a limit of 5 connections. Each Grafana uses at most 2, so about two people can run it at once. `fnol_bi` was created by hand because Grafana cannot sign in with an IAM token; it is not in `deploy/aws/sql/postgres`.

## Changing the dashboards

The JSON in `dashboards/` is generated. Edit `build_dashboards.py`, then:

```sh
python3 deploy/grafana/build_dashboards.py
```

Grafana reloads the files within about 10 seconds. The gold check reimplements `tools/evaluate.py` in SQL against the latest draft for each case and embeds `feedback/dev_gold.json` and the sources in `data/dev/dataset.json`, so regenerate after either file changes. Gemma prices are dashboard variables (default $0.00014 input and $0.0004 output per 1K tokens, on-demand).
