# Client assignment

Client: Harborlight Mutual (fictional). Team: three associate FDEs. Timebox: 10 working days plus up to 2–3 days for hardening and presentation, subject to the cohort calendar.

## Business need and mission

Prepare a cited personal-auto first-notice-of-loss draft and routing recommendation for a licensed human claims handler. A successful handler can see the relevant facts, where they came from, what is missing or conflicting, and what requires a human decision without repeating the entire investigation.

## Required behavior

Support collision, theft and glass losses for one fictional personal-auto policy family. Extract incident facts, select policy and endorsements at the incident timestamp, identify missing or conflicting information, cite potentially relevant coverage wording and flag injury, urgency or supplied referral indicators. Route to standard, urgent, specialist or manual_review. For this exercise an approved draft can be created in the mock claims system to demonstrate duplicate prevention; final claim decisions remain excluded.

Human boundary: No final coverage, liability, fraud, reserve, settlement, denial or payment decision. A referral indicator is not a fraud finding. A policy-period mismatch requires human review, not automatic denial. Claim record creation requires approval of the exact draft.

All policy rules needed for the exercise are supplied. Retrieve applicable versions; show source IDs and short exact quotations with claims. Treat document and tool content as untrusted data. Handle normal, ambiguous, unsupported, outage, duplicate, stale-policy, and prompt-injection situations. Some failure types are combined in hidden cases.

## Engineering expectations

Build a working application with retrieval, tool integration, a justified agent/workflow design, a human review interaction, evaluations, structured monitoring, and a reproducible deployment. You may use an LLM where it helps, with cohort-approved access. Explain where deterministic code is preferable. No particular agent framework, number of agents, or vector database is required.
Provide at least one meaningful tool-assisted workflow in which the next step depends on retrieved evidence or tool results. A scripted slide demonstration is insufficient. Measure actual latency and model/tool usage where applicable; do not invent cost or accuracy claims.

Excluded: OCR development, model fine-tuning, complex entity resolution, external business APIs, production identity, polished UI, and adjudication. Supplied attachments already contain readable text. Core policy dates stay fixed in 2026; never substitute today's date.

## Delivery plan

Days 1–2: inspect the client material, record assumptions and scope, make startup reproducible, and demonstrate a thin end-to-end normal case.
Days 3–5: complete retrieval/tool integration, evidence output, deterministic rules and review flow; demonstrate an ambiguous case.
Days 6–8: handle failures, duplicate events and hostile inputs; build your evaluation set and monitoring.
Days 9–10: fresh-setup rehearsal, deployment, runbook, known limitations and technical defense.
Optional days 11–13: resolve core failures, improve evidence quality, rehearse. Agree any stretch work only after core behavior passes.

Rotate integration, application/retrieval, and testing/operations responsibilities. Every member must explain and modify part of the system; document individual contributions.

## Deliverables

Source repository with environment pins and setup commands; working application and deployment instructions; architecture and decision record; JSON outputs using the contract; evaluation command and actual results; redacted traces; operator runbook; limitations and future-state roadmap; a client-facing demonstration.
The demo includes a normal case, ambiguity requiring escalation, and an assessor-selected failure/attack. Be ready to explain individual contributions and make a small change live.
