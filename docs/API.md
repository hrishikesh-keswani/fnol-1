# Mock enterprise API

Base URL `http://127.0.0.1:8767`. JSON UTF-8. No external network dependency. All data is fictional. One mock service hosts the supplied enterprise read tools and case-record interface.

| Method and path | Behavior |
|---|---|
| GET /health | Dataset name and service health |
| GET /v1/cases | Case IDs and document source IDs; never expected answers |
| GET /v1/cases/CASE_ID/TOOL | Retrieve one enterprise record payload; tools: `policy`, `claims` |
| POST /v1/reviews | Submit `{"case_id":"...","draft":{"case_id":"..."}}`; returns review_id, pending status and draft hash |
| GET /v1/reviews/REVIEW_ID | Poll human decision and review data |
| POST /v1/records | Create approved record; see contract below |
| GET /v1/records/CASE_ID | Fetch live created record for this underlying complaint/loss, including a duplicate intake ID, or 404 |

The case-scoped paths are a deliberate simulator simplification. In production, tools would use tenant, customer, transaction or policy identifiers and real authorization.
Enterprise tool payloads are fixed evidence snapshots taken at intake. In particular, claims history does not mutate when a new mock record is created. Use GET /v1/records/CASE_ID for the live creation ledger and duplicate lookup; it resolves duplicate intake IDs to the same underlying complaint/loss. Evidence citations use snapshot sources; demonstrate ledger state through the audit and operator interaction.
Tool responses include `source_id`; preserve it in citations. Quote text exactly as supplied; for JSON fields, a fragment such as `"amount": 117` is accepted. JSON evidence is normalized with sorted keys and standard spaces in the evaluator.

POST /v1/records body:

```json
{"case_id":"CASE_ID","review_id":"rev_...","draft":{"case_id":"CASE_ID","summary":"Human-reviewed draft"}}
```

Add the `Idempotency-Key` header. Exact replay returns the original record (200); first creation returns 201. Reusing a key for different content returns 409. An already-created underlying complaint/loss returns 409 `duplicate_case` even with a new key or duplicate intake ID. Treat this as an existing record, never fabricate another case.
Approval is bound to the full draft hash. Missing, rejected, wrong-case, or changed-draft approval returns 403. An agent cannot approve through HTTP. Human operator commands are documented in README.
KYC is read-only: every record-creation attempt is forbidden, even if a review was approved.

A configured upstream timeout returns 503 `upstream_timeout` promptly rather than sleeping. This tests the team's error path; it does not test actual network timeout duration. Transient faults fail once; persistent faults fail on every request until reset. Use at most three total attempts, then escalate. Counter state survives service restart. Reset it between independent assessment runs.
Unknown resources return 404. Invalid input returns 400 or 413. No batch processing, authentication, real payment, claim adjudication or customer approval endpoint exists.

## Operator commands

`pending`, `approve`, `reject`, `audit`, and `reset --confirm` accept `--state PATH`.
`serve` accepts `--data PATH`, `--state PATH`, and `--port NUMBER`.
When using a nondefault state, supply the same path to every operator command. Stop clients before resetting.
Operator CLI access is a training simplification. Approval identity and immutable audit storage are future design work, not claims made by this mock.
