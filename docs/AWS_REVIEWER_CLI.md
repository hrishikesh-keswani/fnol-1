# AWS reviewer CLI (accept / reject)

Use this when you want human decisions to hit **AWS Postgres** (and Grafana), not local SQLite.

Do **not** use bare `tools/runtime.py approve` for AWS unless `FNOL_DATABASE_URL` is pointed at the RDS tunnel. Default runtime state is local.

## Prerequisites

- AWS CLI v2 + Session Manager plugin
- SSO profile (examples use `atharv`)
- uv with the `postgres` (and on ECS, `aws`) group

```sh
aws sso login --profile atharv
```

Ignore `aws login` errors on profile `default` if that profile has access keys. Use `AWS_PROFILE=atharv`.

If `ecs execute-command` says `SessionManagerPlugin is not found`, the binary is often installed but not on `PATH`:

```sh
export PATH="/usr/local/sessionmanagerplugin/bin:$PATH"
# optional permanent: add that line to ~/.zshrc
```

## 1. Start the AWS-backed reviewer

Leave this terminal running:

```sh
AWS_PROFILE=atharv uv run --locked --group postgres python tools/reviewer_aws.py
```

Success looks like:

```text
reviewer_aws: tunnel already on 127.0.0.1:15432   # or "starting SSM tunnel…" then "tunnel ready"
reviewer_aws: serving AWS Postgres via tunnel on http://127.0.0.1:8090/
reviewer HTTP on http://127.0.0.1:8090 (postgres)
```

UI: http://127.0.0.1:8090/

## 2. If port 8090 is already in use

Error: `OSError: [Errno 48] Address already in use`

Find and stop the old listener:

```sh
lsof -nP -iTCP:8090 -sTCP:LISTEN
kill <PID>
```

Or check whether the existing process is already usable:

```sh
curl -s -H "X-Fnol-Roles: Claims.Reviewer" \
  http://127.0.0.1:8090/v1/pending | python3 -m json.tool
```

If that returns JSON, you can skip restarting.

Optional: run on another port instead of killing:

```sh
FNOL_HTTP_PORT=8091 AWS_PROFILE=atharv \
  uv run --locked --group postgres python tools/reviewer_aws.py
```

Then use `8091` in the curl commands below.

## 3. List pending reviews (new terminal)

```sh
curl -s -H "X-Fnol-Roles: Claims.Reviewer" \
  http://127.0.0.1:8090/v1/pending | python3 -m json.tool
```

Note each item’s `id` (e.g. `rev_…`) and `case_id`.

Empty `[]` means nothing is waiting. Submit drafts from the **analyst ECS task** first (see below).

## 4. Accept or reject

```sh
# accept
curl -s -X POST \
  -H "X-Fnol-Roles: Claims.Reviewer" \
  -H "X-Fnol-Reviewer: atharv" \
  http://127.0.0.1:8090/v1/approve/REVIEW_ID

# reject
curl -s -X POST \
  -H "X-Fnol-Roles: Claims.Reviewer" \
  -H "X-Fnol-Reviewer: atharv" \
  http://127.0.0.1:8090/v1/reject/REVIEW_ID
```

Replace `REVIEW_ID` with a real `rev_…` value. Expected body includes `"status":"approved"` or `"status":"rejected"`.

Re-list pending; that review should be gone. Grafana refreshes about every minute.

## 5. Refill AWS after blank (ECS analyst): prepare + submit

Laptop `python -m fnol_analyst` **does not** write the AWS DB. Run prepare/submit **inside** the Fargate analyst task.

There is no `submit --all`. `prepare` covers all cases in one command; `submit` needs `--case-id` (use the loop below).

### A. Open an ECS shell on the analyst task

```sh
export PATH="/usr/local/sessionmanagerplugin/bin:$PATH"

TASK=$(aws ecs list-tasks --cluster fnol --service-name analyst \
  --desired-status RUNNING --region us-east-1 --profile atharv \
  --query 'taskArns[0]' --output text)

echo "$TASK"   # must be a task ARN, not None

aws ecs execute-command --cluster fnol --task "$TASK" \
  --container analyst --interactive --command "/bin/sh" \
  --region us-east-1 --profile atharv
```

You should see a `#` prompt inside the container.

### B. Process all 12 cases (ingest → policy → route → cite → build)

Do **not** use bare `python` (missing `boto3`). Use the same uv groups as the task definition:

```sh
uv run --locked --group aws --group postgres --no-dev python -m fnol_analyst prepare
```

`Bytecode compiled …` is only uv loading deps. Wait for JSON like:

```text
{
  "cases": 12,
  "drafts": [ { "draft_id": "draft_…", "case_id": "AUTO-D01", "draft_hash": "…" }, … ],
  "exported": "submissions/results.json"
}
```

- **`draft_id`** — local draft record id (filing / idempotency).
- **`draft_hash`** — fingerprint of the exact draft; human approval is bound to this hash.

`prepare` alone updates Grafana pipeline data. It does **not** put cases in the human review queue.

### C. Put all 12 drafts into the review queue

Still in the ECS shell (no `submit --all` in the CLI):

```sh
for c in AUTO-D01 AUTO-D02 AUTO-D03 AUTO-D04 AUTO-D05 AUTO-D06 \
         AUTO-D07 AUTO-D08 AUTO-D09 AUTO-D10 AUTO-D11 AUTO-D12; do
  uv run --locked --group aws --group postgres --no-dev \
    python -m fnol_analyst submit --case-id "$c"
done
```

Each case should print a small JSON with `review_id` / `status`. Single case example:

```sh
uv run --locked --group aws --group postgres --no-dev \
  python -m fnol_analyst submit --case-id AUTO-D02
```

### D. Back on your laptop — list and decide

With `reviewer_aws.py` still running (§1):

```sh
curl -s -H "X-Fnol-Roles: Claims.Reviewer" \
  http://127.0.0.1:8090/v1/pending | python3 -m json.tool
```

Then approve/reject (§4).

## Quick demo picks

| Case | What it shows |
|------|----------------|
| AUTO-D02 | urgent / injury |
| AUTO-D04 | missing required information |
| AUTO-D05 | ambiguous / no unique policy |
| AUTO-D08 | prompt-injection blocked → manual_review |
| AUTO-D07 | claims history unavailable (after submit) |
| AUTO-D10 | duplicate delivery (hard block on file after approve) |
| AUTO-D11 | repair-amount conflict → specialist (Grafana “Conflicting evidence” stays 0; that panel counts `conflicts[]` only) |

## 6. Clear / blank the AWS DB

Wipes **shared** FNOL data tables only (input, output, trace, mock). Does **not** delete the RDS instance, schemas, roles, or migration ledger. **This clears Grafana numbers for everyone** — only do it when the team agrees.

Uses `fnol_migrate` (has truncate rights). `fnol_reviewer` cannot blank all tables.

### A. Tunnel must be up first

In a dedicated terminal (leave it running):

```sh
# free stale reviewer if needed
lsof -nP -iTCP:8090 -sTCP:LISTEN
kill <PID>   # only if something is on 8090

AWS_PROFILE=atharv uv run --locked --group postgres python tools/reviewer_aws.py
```

Confirm the tunnel:

```sh
lsof -nP -iTCP:15432 -sTCP:LISTEN
```

If you see `Connection refused` / timeout on blank, the tunnel died — kill whatever was on `8090`, re-run `reviewer_aws.py`, then continue.

### B. Fresh IAM token + blank (other terminal)

IAM tokens expire in ~15 minutes. Generate the token and blank in the **same** shell, right before running:

```sh
aws sso login --profile atharv

RDS_HOST=fnol-postgres.cc92c6i402lj.us-east-1.rds.amazonaws.com
TOKEN=$(aws rds generate-db-auth-token \
  --hostname "$RDS_HOST" --port 5432 --region us-east-1 \
  --username fnol_migrate --profile atharv)
ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$TOKEN")

unset FNOL_RDS_HOST FNOL_RDS_IAM_AUTH
export FNOL_DATABASE_URL="postgresql://fnol_migrate:${ENC}@127.0.0.1:15432/fnol?sslmode=require"
export FNOL_PG_NAMESPACE=shared

# must print: 127.0.0.1:15432/fnol?sslmode=require
python3 -c "import os; print(os.environ['FNOL_DATABASE_URL'].split('@')[-1])"

uv run --locked --group postgres python tools/blank_state.py
```

Success:

```text
blanked input.input_revisions, input.procedure_sources, input.tool_payloads, input.decisions, output.drafts, trace.trace_events, mock.reviews, mock.records, mock.keys, mock.calls, mock.audit
```

Then:

```sh
curl -s -H "X-Fnol-Roles: Claims.Reviewer" \
  http://127.0.0.1:8090/v1/pending | python3 -m json.tool
```

Expect `[]`. Then refill with §5 (ECS `prepare` + submit loop).

### Local mock only (does **not** clear AWS)

```sh
uv run --locked python tools/runtime.py reset --confirm
```

## Stop

Ctrl-C the `reviewer_aws.py` terminal. Optional: kill the SSM tunnel on `15432` if nothing else needs it (`lsof -nP -iTCP:15432 -sTCP:LISTEN`).
