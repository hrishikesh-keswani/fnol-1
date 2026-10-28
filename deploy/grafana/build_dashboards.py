"""Generate the two FNOL Grafana dashboards from code.

    python3 deploy/grafana/build_dashboards.py

Writes dashboards/fnol-executive.json (leadership) and dashboards/fnol-operations.json
(team detail). The gold check mirrors tools/evaluate.py, using feedback/dev_gold.json and
data/dev/dataset.json, so re-run this script whenever either file changes.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE / "dashboards"

PG = {"type": "grafana-postgresql-datasource", "uid": "fnol-postgres"}
CW = {"type": "cloudwatch", "uid": "fnol-cloudwatch"}

GOLD = json.loads((REPO / "feedback/dev_gold.json").read_text())
SOURCES = json.loads((REPO / "data/dev/dataset.json").read_text())["sources"]


def sql_json(value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if re.search(r"\$\w|\$\{|\[\[\w+", text):
        raise ValueError("literal contains text Grafana would treat as a template variable")
    return "'" + text.replace("'", "''") + "'::jsonb"


# ---------------------------------------------------------------- shared SQL

RECEIVED = """received AS (
  SELECT case_id, received_at FROM (
    SELECT case_id, min(ingested_at::timestamptz) AS received_at
    FROM input.input_revisions
    GROUP BY case_id
  ) first_seen
  WHERE $__timeFilter(received_at)
)"""

CLAIMS = "WITH " + RECEIVED + """,
latest AS (
  SELECT DISTINCT ON (case_id) case_id, status, outcome, review_id, created_at
  FROM output.drafts
  ORDER BY case_id, seq DESC
),
route AS (
  SELECT DISTINCT ON (case_id) case_id, body_json::jsonb AS b
  FROM input.decisions
  WHERE kind = 'route'
  ORDER BY case_id, input_revision DESC
),
claims AS (
  SELECT r.case_id, r.received_at, l.created_at::timestamptz AS drafted_at,
    rv.status AS review_status, rv.reviewer, ro.b AS decision,
    CASE
      WHEN l.status = 'created' OR rec.case_id IS NOT NULL THEN 'Submitted'
      WHEN l.outcome = 'duplicate' THEN 'Duplicate, not resubmitted'
      WHEN rv.status = 'rejected' OR l.outcome = 'rejected' THEN 'Rejected'
      WHEN l.outcome IN ('approval_mismatch', 'idempotency_conflict', 'hash_mismatch') THEN 'Needs attention'
      WHEN rv.status = 'approved' THEN 'Accepted, awaiting submission'
      WHEN rv.status = 'pending' THEN 'Awaiting reviewer'
      ELSE 'In automated processing'
    END AS stage
  FROM received r
  LEFT JOIN latest l ON l.case_id = r.case_id
  LEFT JOIN mock.reviews rv ON rv.id = l.review_id
  LEFT JOIN mock.records rec ON rec.case_id = r.case_id
  LEFT JOIN route ro ON ro.case_id = r.case_id
)
"""

STAGES = [
    ("In automated processing", "blue"),
    ("Awaiting reviewer", "orange"),
    ("Accepted, awaiting submission", "yellow"),
    ("Submitted", "green"),
    ("Rejected", "red"),
    ("Duplicate, not resubmitted", "purple"),
    ("Needs attention", "dark-red"),
]
STAGE_VALUES = ", ".join(f"({i}, '{name}')" for i, (name, _) in enumerate(STAGES, 1))

ROUTE_LABEL = """CASE {b} ->> 'route'
    WHEN 'standard' THEN 'Standard handling'
    WHEN 'urgent' THEN 'Urgent'
    WHEN 'specialist' THEN 'Specialist team'
    WHEN 'manual_review' THEN 'Manual review'
    ELSE coalesce({b} ->> 'route', 'Not sorted') END"""

CLASS_LABEL = "initcap(coalesce({b} ->> 'classification', 'Not sorted'))"

STEP_LABEL = """CASE step WHEN '1' THEN '01 Intake' WHEN '2' THEN '02 Policy lookup' WHEN '3' THEN '03 Rules + injection screen'
    WHEN '4' THEN '04 Citations' WHEN '5' THEN '05 Draft built' WHEN '6' THEN '06 Gemma summary' WHEN '7' THEN '07 Safety scan'
    WHEN '8' THEN '08 Draft recorded' WHEN '9' THEN '09 Review requested' WHEN '10' THEN '10 Review polled'
    WHEN '11' THEN '11 Record lookup' WHEN '12' THEN '12 Record submitted' ELSE step END"""

# Gemma is called in step 3 (prompt-injection screen) and step 6 (summary generation).
# Step 7 repeats step 6's token counts and overlaps its timer, so it is excluded from
# cost and processing time. Totals are the sum of the two Gemma steps.
INJECTION_ROWS = "model_id IS NOT NULL AND step = '3'"
SUMMARY_ROWS = "model_id IS NOT NULL AND step = '6'"
MODEL_ROWS = "model_id IS NOT NULL AND step IN ('3', '6')"
PROCESSING_ROWS = "case_id IS NOT NULL AND step::int <= 9 AND step <> '7'"
TRACE_TIME = "$__timeFilter(started_at::timestamptz)"
SPLIT_FIELDS = (("Prompt injection", INJECTION_ROWS), ("Summary", SUMMARY_ROWS), ("Total", MODEL_ROWS))


def token_expr(predicate, part="both"):
    if part == "input":
        inner = "coalesce(input_tokens, 0)"
    elif part == "output":
        inner = "coalesce(output_tokens, 0)"
    else:
        inner = "coalesce(input_tokens, 0) + coalesce(output_tokens, 0)"
    return f"coalesce(sum({inner}) FILTER (WHERE {predicate}), 0)"


def cost_expr(predicate):
    filt = f" FILTER (WHERE {predicate})"
    return (
        "(coalesce(sum(input_tokens)" + filt + ", 0) / 1000.0 * ${input_price}"
        " + coalesce(sum(output_tokens)" + filt + ", 0) / 1000.0 * ${output_price})"
    )


def split_select(metric):
    return ", ".join(f'{metric(predicate)} AS "{label}"' for label, predicate in SPLIT_FIELDS)


def split_rounded(metric, decimals):
    """Round each part, then show Total as their sum so the three figures add up."""
    rendered = []
    selected = []
    for label, predicate in SPLIT_FIELDS:
        if label == "Total":
            continue
        expr = f"round(({metric(predicate)})::numeric, {decimals})"
        rendered.append(expr)
        selected.append(f'{expr} AS "{label}"')
    selected.append(f'({" + ".join(rendered)}) AS "Total"')
    return ", ".join(selected)


def input_tokens(predicate):
    return token_expr(predicate, "input")


def output_tokens(predicate):
    return token_expr(predicate, "output")


def tokens_per_claim(predicate):
    return f"{token_expr(predicate)}::numeric / NULLIF(count(DISTINCT case_id), 0)"


def cost_per_thousand(predicate):
    return f"1000 * {cost_expr(predicate)} / NULLIF(count(DISTINCT case_id), 0)"


def period_cost(predicate):
    return cost_expr(predicate)

GOLD_CHECKS = [
    ("classification", "Claim type"),
    ("routing", "Routing"),
    ("policy_selection", "Policies"),
    ("facts", "Facts"),
    ("missing_information", "Missing info"),
    ("deadline", "Deadline"),
    ("citations_valid", "Citations"),
    ("required_evidence", "Evidence"),
    ("narrative_present", "Narrative"),
    ("no_restricted_values", "Privacy"),
    ("targeted_questions", "Questions"),
]

WS = r"E' \t\n\r\f\v'"

GOLD_BASE = "WITH " + RECEIVED + f""",
gold AS (SELECT key AS case_id, value AS g FROM jsonb_each({sql_json(GOLD)})),
sources AS (SELECT key AS source_id, value #>> '{{}}' AS body FROM jsonb_each({sql_json(SOURCES)})),
drafts AS (
  SELECT DISTINCT ON (case_id) case_id, draft_json::jsonb AS d
  FROM output.drafts
  ORDER BY case_id, seq DESC
),
cites AS (
  SELECT dr.case_id, c.value AS c,
    coalesce(jsonb_typeof(c.value -> 'source_id') = 'string' AND jsonb_typeof(c.value -> 'quote') = 'string'
      AND btrim(c.value ->> 'quote', {WS}) <> '' AND strpos(s.body, c.value ->> 'quote') > 0, false) AS valid
  FROM drafts dr
  CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(dr.d -> 'citations') = 'array' THEN dr.d -> 'citations' ELSE '[]' END) c(value)
  LEFT JOIN sources s ON s.source_id = c.value ->> 'source_id'
),
scored AS (
  SELECT gd.case_id, dr.d IS NOT NULL AS drafted,
    coalesce(gd.g -> 'classification' @> jsonb_build_array(dr.d -> 'classification'), false) AS classification,
    coalesce(gd.g -> 'routing' @> jsonb_build_array(dr.d -> 'routing'), false) AS routing,
    ARRAY(SELECT x FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(dr.d -> 'policy_ids') = 'array' THEN dr.d -> 'policy_ids' ELSE '[]' END) x ORDER BY x COLLATE "C")
      = ARRAY(SELECT x FROM jsonb_array_elements_text(gd.g -> 'policy_ids') x ORDER BY x COLLATE "C") AS policy_selection,
    NOT EXISTS (SELECT 1 FROM jsonb_each(gd.g -> 'facts') f
      WHERE NOT coalesce(jsonb_typeof(dr.d -> 'facts') = 'object' AND dr.d -> 'facts' ? f.key AND dr.d -> 'facts' -> f.key = f.value, false)) AS facts,
    ARRAY(SELECT DISTINCT x FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(dr.d -> 'missing_information') = 'array' THEN dr.d -> 'missing_information' ELSE '[]' END) x ORDER BY x)
      = ARRAY(SELECT DISTINCT x FROM jsonb_array_elements_text(gd.g -> 'missing_information') x ORDER BY x) AS missing_information,
    coalesce(dr.d ? 'deadline' AND dr.d -> 'deadline' = gd.g -> 'deadline', false) AS deadline,
    EXISTS (SELECT 1 FROM cites c WHERE c.case_id = gd.case_id)
      AND NOT EXISTS (SELECT 1 FROM cites c WHERE c.case_id = gd.case_id AND NOT c.valid) AS citations_valid,
    NOT EXISTS (SELECT 1 FROM jsonb_array_elements(gd.g -> 'evidence_groups') grp(v)
      WHERE NOT EXISTS (SELECT 1 FROM cites c WHERE c.case_id = gd.case_id AND c.valid AND grp.v @> jsonb_build_array(c.c -> 'source_id'))) AS required_evidence,
    coalesce(jsonb_typeof(dr.d -> 'narrative') = 'string' AND length(btrim(dr.d ->> 'narrative', {WS})) >= 20, false) AS narrative_present,
    NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(coalesce(gd.g -> 'restricted_values', '[]')) rv(v)
      WHERE strpos(dr.d::text, rv.v) > 0) AS no_restricted_values,
    NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(gd.g -> 'missing_information') m(field)
      WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(dr.d -> 'follow_up_questions') = 'array' THEN dr.d -> 'follow_up_questions' ELSE '[]' END) q(v)
        WHERE jsonb_typeof(q.v) = 'object' AND q.v ->> 'field' = m.field
          AND jsonb_typeof(q.v -> 'question') = 'string' AND length(btrim(q.v ->> 'question', {WS})) >= 10)) AS targeted_questions
  FROM gold gd
  JOIN received USING (case_id)
  LEFT JOIN drafts dr USING (case_id)
),
results AS (
  SELECT *, drafted AND {" AND ".join(k for k, _ in GOLD_CHECKS)} AS passed FROM scored
)
"""


# ---------------------------------------------------------------- panel helpers

def target(sql, ref="A"):
    return {"refId": ref, "datasource": PG, "editorMode": "code", "format": "table", "rawQuery": True, "rawSql": sql}


def cloudwatch(namespace, metric, dimensions, statistic, label):
    return {"refId": "A", "datasource": CW, "queryMode": "Metrics", "metricQueryType": 0, "metricEditorMode": 0,
            "region": "default", "namespace": namespace, "metricName": metric, "dimensions": dimensions,
            "matchExact": True, "statistic": statistic, "period": "", "id": "", "expression": "", "label": label}


class Layout:
    def __init__(self):
        self.panels = []
        self.y = 0

    def add(self, panel):
        panel["id"] = len(self.panels) + 1
        self.panels.append(panel)
        return panel

    def row(self, title):
        self.add({"type": "row", "title": title, "collapsed": False, "panels": [],
                  "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.y += 1

    def place(self, panels, h):
        x = 0
        for panel, w in panels:
            panel["gridPos"] = {"h": h, "w": w, "x": x, "y": self.y}
            self.add(panel)
            x += w
        self.y += h


def _split_overrides(total_color):
    return [
        by_name("Prompt injection", ("color", {"mode": "fixed", "fixedColor": "orange"})),
        by_name("Summary", ("color", {"mode": "fixed", "fixedColor": "purple"})),
        by_name("Total", ("color", {"mode": "fixed", "fixedColor": total_color})),
    ]


def stat(title, sql, description, color="blue", unit=None, decimals=0, thresholds=None, split=False, value_size=None):
    defaults = {"color": {"mode": "fixed", "fixedColor": color}, "decimals": decimals}
    if thresholds:
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    if unit:
        defaults["unit"] = unit
    options = {"colorMode": "background", "graphMode": "none", "justifyMode": "center",
               "textMode": "value_and_name" if split else "value",
               "wideLayout": not split, "orientation": "vertical" if split else "auto",
               "showPercentChange": False,
               "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
    if value_size:
        options["text"] = {"valueSize": value_size}
    return {"type": "stat", "title": title, "description": description, "datasource": PG,
            "fieldConfig": {"defaults": defaults, "overrides": _split_overrides(color) if split else []},
            "options": options, "targets": [target(sql)]}


def bars(title, sql, description, x_field, color="blue", unit=None, orientation="horizontal",
         stacked=False, overrides=None, color_by=None, legend=False, rotate=0):
    defaults = {"min": 0, "decimals": 0, "color": {"mode": "fixed", "fixedColor": color},
                "custom": {"fillOpacity": 85, "lineWidth": 0, "gradientMode": "none", "axisSoftMin": 0}}
    if unit:
        defaults["unit"] = unit
        defaults.pop("decimals")
    if color_by or legend:
        defaults["color"] = {"mode": "palette-classic"} if legend else defaults["color"]
    options = {"xField": x_field, "orientation": orientation, "showValue": "always", "barWidth": 0.7,
               "groupWidth": 0.75, "stacking": "normal" if stacked else "none", "xTickLabelRotation": rotate,
               "legend": {"showLegend": legend, "displayMode": "list", "placement": "bottom"},
               "tooltip": {"mode": "multi" if legend else "single", "sort": "none"}}
    if color_by:
        options["colorByField"] = color_by
    return {"type": "barchart", "title": title, "description": description, "datasource": PG,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": options, "targets": [target(sql)]}


def table(title, sql, description, overrides=None, sort=None, transformations=None):
    panel = {"type": "table", "title": title, "description": description, "datasource": PG,
             "fieldConfig": {"defaults": {"custom": {"align": "left", "inspect": False, "filterable": False, "minWidth": 60}},
                             "overrides": overrides or []},
             "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}, "sortBy": sort or []},
             "targets": [target(sql)]}
    if transformations:
        panel["transformations"] = transformations
    return panel


def by_name(name, *props):
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props]}


def by_regex(regex, *props):
    return {"matcher": {"id": "byRegexp", "options": regex}, "properties": [{"id": k, "value": v} for k, v in props]}


def color_map(pairs):
    return [{"type": "value", "options": {text: {"color": color, "index": i} for i, (text, color) in enumerate(pairs)}}]


STAGE_COLORS = color_map(STAGES)
COLOR_TEXT = {"type": "color-text"}

PRICE_VARS = [
    {"name": "input_price", "label": "Gemma input $ per 1K tokens", "type": "textbox", "query": "0.00014",
     "current": {"text": "0.00014", "value": "0.00014"}, "options": [{"selected": True, "text": "0.00014", "value": "0.00014"}]},
    {"name": "output_price", "label": "Gemma output $ per 1K tokens", "type": "textbox", "query": "0.0004",
     "current": {"text": "0.0004", "value": "0.0004"}, "options": [{"selected": True, "text": "0.0004", "value": "0.0004"}]},
]


def dashboard(uid, title, description, layout, links, hide_vars, time_from="now-30d"):
    variables = [dict(v, hide=2 if hide_vars else 0) for v in PRICE_VARS]
    return {
        "uid": uid, "title": title, "description": description, "tags": ["fnol"],
        "editable": True, "graphTooltip": 1, "liveNow": False, "refresh": "1m", "schemaVersion": 41,
        "time": {"from": time_from, "to": "now"}, "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
        "timezone": "browser", "fiscalYearStartMonth": 0, "weekStart": "",
        "annotations": {"list": []}, "templating": {"list": variables},
        "links": [{"title": t, "type": "link", "url": u, "icon": "dashboard", "targetBlank": False,
                   "asDropdown": False, "includeVars": False, "keepTime": True, "tags": [], "tooltip": ""} for t, u in links],
        "panels": layout.panels, "version": 1,
    }


# ---------------------------------------------------------------- shared panels

def stage_count(stage):
    return CLAIMS + f"SELECT count(*) FILTER (WHERE stage = '{stage}') AS claims FROM claims"


# Same pixel size on every glance tile. Auto-sizing shrinks "92%" below "12".
GLANCE_VALUE = 44


def glance(title, sql, description, color="blue", unit=None, decimals=0, thresholds=None):
    return stat(title, sql, description, color, unit=unit, decimals=decimals, thresholds=thresholds, value_size=GLANCE_VALUE)


P_RECEIVED = lambda: glance("Received", CLAIMS + "SELECT count(*) AS claims FROM claims",
                            "Claims that entered intake in the selected period. Processing, Pending, Duplicate, Approved not processed, Submitted, Rejected, and Needs attention add up to this number.",
                            "blue")
P_PROCESSING = lambda: glance("Processing", stage_count("In automated processing"),
                              "Still inside the automated pipeline. No draft is waiting on a person yet.", "light-blue")
P_PENDING = lambda: glance("Pending", stage_count("Awaiting reviewer"),
                           "A draft is with a reviewer and no decision has been made.", "orange")
P_DUPLICATE = lambda: glance("Duplicate", stage_count("Duplicate, not resubmitted"),
                             "A reviewer approved the draft, and the claims system refused a second record because this loss was already on file.",
                             "purple")
P_NOT_PROCESSED = lambda: glance("Approved, not processed", stage_count("Accepted, awaiting submission"),
                                 "A reviewer approved the draft, and submission did not finish, so nothing was written to the claims system.",
                                 "yellow")
P_SUBMITTED = lambda: glance("Submitted", stage_count("Submitted"),
                             "Approved drafts recorded in the claims system.", "green")
P_REJECTED = lambda: glance("Rejected", stage_count("Rejected"),
                            "A reviewer rejected the draft. Nothing was recorded in the claims system.", "red")
P_ATTENTION = lambda: glance("Needs attention", stage_count("Needs attention"),
                             "The approval and the draft disagree, an idempotency key was reused for a different body, or the draft hash does not match.",
                             "dark-red")
GOLD_THRESHOLDS = [{"color": "red", "value": None}, {"color": "orange", "value": 80}, {"color": "green", "value": 95}]
SUCCESS_THRESHOLDS = [{"color": "red", "value": None}, {"color": "orange", "value": 90}, {"color": "green", "value": 95}]

SUCCESS_SQL = f"""SELECT round(100.0 * count(*) FILTER (WHERE status = 'ok') / NULLIF(count(*), 0), 1) AS success_pct
FROM trace.trace_events
WHERE {TRACE_TIME}"""

PROCESSING_SQL = f"""SELECT round(avg(seconds)::numeric, 1) AS seconds FROM (
  SELECT case_id, sum(duration_ms) / 1000.0 AS seconds
  FROM trace.trace_events
  WHERE {PROCESSING_ROWS} AND {TRACE_TIME}
  GROUP BY case_id
) per_claim"""

COST_TOTAL_SQL = f"SELECT {split_rounded(period_cost, 6)} FROM trace.trace_events WHERE {MODEL_ROWS} AND {TRACE_TIME}"
COST_PER_THOUSAND_SQL = f"SELECT {split_rounded(cost_per_thousand, 4)} FROM trace.trace_events WHERE {MODEL_ROWS} AND {TRACE_TIME}"
MODEL_FROM = f"FROM trace.trace_events WHERE {MODEL_ROWS} AND {TRACE_TIME}"


def journey():
    return bars("Claim journey", CLAIMS + f"""SELECT s.stage, count(c.case_id) AS claims
FROM (VALUES {STAGE_VALUES}) AS s(ord, stage)
LEFT JOIN claims c ON c.stage = s.stage
GROUP BY s.ord, s.stage
ORDER BY s.ord""", "Every claim received in the period, by its current stage. The stages add up to Received.",
                "stage", color_by="stage", overrides=[by_name("stage", ("mappings", STAGE_COLORS))])


def routing_donut():
    return {"type": "piechart", "title": "How claims were sorted",
            "description": "Handling path the rules engine chose for each claim.", "datasource": PG,
            "fieldConfig": {"defaults": {"decimals": 0, "color": {"mode": "palette-classic"}}, "overrides": []},
            "options": {"pieType": "donut", "displayLabels": ["value"],
                        "legend": {"displayMode": "table", "placement": "right", "showLegend": True, "values": ["value", "percent"]},
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
                        "tooltip": {"mode": "single", "sort": "none"}},
            "targets": [target(CLAIMS + f"""SELECT {ROUTE_LABEL.format(b='decision')} AS route, count(*) AS claims
FROM claims
WHERE decision IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC, 1""")]}


def waiting_table():
    return table("Claims waiting on a person", CLAIMS + f"""SELECT
  case_id AS "Claim",
  stage AS "Stage",
  {ROUTE_LABEL.format(b='decision')} AS "Route",
  round((extract(epoch FROM now() - CASE
      WHEN stage = 'Accepted, awaiting submission' THEN coalesce(
        (SELECT max(a.at::timestamptz) FROM mock.audit a WHERE a.case_id = claims.case_id AND a.event = 'human_approved'),
        drafted_at)
      ELSE drafted_at END) / 3600)::numeric, 1) AS "Waiting (hours)"
FROM claims
WHERE stage IN ('Needs attention', 'Accepted, awaiting submission', 'Awaiting reviewer')
ORDER BY CASE stage WHEN 'Needs attention' THEN 0 WHEN 'Accepted, awaiting submission' THEN 1 ELSE 2 END, "Waiting (hours)" DESC""",
                 "Claims that need a reviewer decision, or accepted claims that still have to be submitted to the claims system. "
                 "Waiting time runs from when the draft was ready, or from acceptance for accepted claims.",
                 overrides=[by_name("Claim", ("custom.width", 100)),
                            by_name("Waiting (hours)", ("decimals", 1), ("custom.width", 130)),
                            by_name("Stage", ("mappings", STAGE_COLORS), ("custom.cellOptions", COLOR_TEXT))])


def risks():
    return bars("Risks caught before submission", CLAIMS + """, d AS (SELECT decision AS b FROM claims WHERE decision IS NOT NULL)
SELECT control, claims FROM (
  SELECT 1 AS ord, 'Prompt-injection attempts blocked' AS control,
         count(*) FILTER (WHERE b ->> 'injection_model_status' = 'injection' OR b ->> 'injection_field_status' = 'injection') AS claims FROM d
  UNION ALL SELECT 2, 'Duplicate submissions caught', count(*) FILTER (WHERE (b ->> 'duplicate_delivery')::boolean) FROM d
  UNION ALL SELECT 3, 'Missing required information',
         count(*) FILTER (WHERE jsonb_typeof(b -> 'missing_information') = 'array' AND jsonb_array_length(b -> 'missing_information') > 0) FROM d
  UNION ALL SELECT 4, 'Conflicting evidence',
         count(*) FILTER (WHERE jsonb_typeof(b -> 'conflicts') = 'array' AND jsonb_array_length(b -> 'conflicts') > 0) FROM d
  UNION ALL SELECT 5, 'Claim history unavailable', count(*) FILTER (WHERE (b ->> 'claims_available')::boolean = false) FROM d
) t
ORDER BY ord""", "Claims the automation flagged instead of processing blindly. Every flagged claim goes to a person.",
                "control", color="orange")


GOLD_PASS_SQL = GOLD_BASE + "SELECT round(100.0 * count(*) FILTER (WHERE passed) / NULLIF(count(*), 0), 1) AS pass_pct FROM results"
GOLD_DESC = ("Share of benchmark claims whose AI-prepared draft passes every automated check against the gold answers "
             "(feedback/dev_gold.json): classification, routing, policies, facts, missing information, deadline, "
             "citations, evidence, narrative, privacy and follow-up questions. Same rules as tools/evaluate.py.")


# ---------------------------------------------------------------- executive summary

def executive():
    L = Layout()
    L.row("Claims at a glance")
    # Two rows of the same tile size so "12" and "92%" use one font and one box.
    L.place([
        (P_RECEIVED(), 4), (P_PROCESSING(), 4), (P_PENDING(), 4),
        (P_DUPLICATE(), 4), (P_NOT_PROCESSED(), 4), (P_SUBMITTED(), 4),
    ], h=6)
    L.place([
        (P_REJECTED(), 4), (P_ATTENTION(), 4),
        (glance("Gold accuracy", GOLD_PASS_SQL, GOLD_DESC, unit="percent", decimals=0, thresholds=GOLD_THRESHOLDS), 4),
    ], h=6)
    L.place([(journey(), 14), (routing_donut(), 10)], h=9)
    L.row("Speed, cost and reliability")
    L.place([
        (stat("Processing time per claim", PROCESSING_SQL,
              "Average automated work per claim, from intake until the draft reaches a reviewer. Human review time is excluded.",
              "blue", unit="s", decimals=1), 6),
        (stat("AI cost per 1,000 claims", COST_PER_THOUSAND_SQL,
              "Gemma 4 31B (Amazon Bedrock) cost for every 1,000 claims, at standard on-demand pricing. "
              "Prompt injection is the step 3 screen, summary is step 6, and total is the two combined.",
              "green", unit="currencyUSD", decimals=4, split=True), 6),
        (stat("Total AI cost", COST_TOTAL_SQL,
              "Model spend for claims processed in the period. Prompt injection is the step 3 screen, "
              "summary is step 6, and total is the two combined.",
              "green", unit="currencyUSD", decimals=6, split=True), 6),
        (stat("Automated step success rate", SUCCESS_SQL,
              "Share of automated pipeline steps that succeeded. Failed steps are retried or the claim is routed to a person.",
              unit="percent", decimals=1, thresholds=SUCCESS_THRESHOLDS), 6),
    ], h=8)
    L.row("Risk controls and follow-up")
    L.place([(risks(), 12), (waiting_table(), 12)], h=9)
    return dashboard("fnol-executive", "FNOL Executive Summary",
                     "Leadership view of first notice of loss claims: volume, outcomes, quality, speed and AI cost.",
                     L, [("Operations detail", "/d/fnol-ops")], hide_vars=True)


# ---------------------------------------------------------------- operations and AI performance

def operations():
    L = Layout()

    L.row("Claim detail")
    L.place([(table("Claims register", CLAIMS + f""", usage AS (
  SELECT case_id,
    sum(duration_ms) FILTER (WHERE {PROCESSING_ROWS}) / 1000.0 AS seconds,
    {token_expr(INJECTION_ROWS)} AS injection_tokens,
    {token_expr(SUMMARY_ROWS)} AS summary_tokens,
    {token_expr(MODEL_ROWS)} AS tokens,
    {cost_expr(INJECTION_ROWS)} AS injection_cost,
    {cost_expr(SUMMARY_ROWS)} AS summary_cost,
    {cost_expr(MODEL_ROWS)} AS cost
  FROM trace.trace_events
  WHERE case_id IS NOT NULL
  GROUP BY case_id
)
SELECT
  c.case_id AS "Claim",
  c.received_at AS "Received",
  {CLASS_LABEL.format(b='c.decision')} AS "Classification",
  {ROUTE_LABEL.format(b='c.decision')} AS "Route",
  c.stage AS "Stage",
  CASE c.review_status WHEN 'approved' THEN 'Accepted' WHEN 'rejected' THEN 'Rejected' WHEN 'pending' THEN 'Pending' ELSE '-' END AS "Reviewer decision",
  coalesce(c.reviewer, '-') AS "Reviewer",
  round(u.seconds::numeric, 1) AS "Processing (s)",
  coalesce(u.injection_tokens, 0) AS "Prompt injection tokens",
  coalesce(u.summary_tokens, 0) AS "Summary tokens",
  coalesce(u.tokens, 0) AS "Total tokens",
  round(coalesce(u.injection_cost, 0)::numeric, 6) AS "Prompt injection AI cost",
  round(coalesce(u.summary_cost, 0)::numeric, 6) AS "Summary AI cost",
  round(coalesce(u.injection_cost, 0)::numeric, 6) + round(coalesce(u.summary_cost, 0)::numeric, 6) AS "Total AI cost"
FROM claims c
LEFT JOIN usage u ON u.case_id = c.case_id
ORDER BY c.case_id""", "One row per claim received in the period. Token counts and AI cost are split between the prompt-injection screen (step 3) and summary generation (step 6). Totals add those two together. Step 7 repeats the summary tokens and is excluded.",
        overrides=[by_name("Stage", ("mappings", STAGE_COLORS), ("custom.cellOptions", COLOR_TEXT)),
                   by_name("Reviewer decision", ("mappings", color_map([("Accepted", "green"), ("Rejected", "red"), ("Pending", "orange")])),
                           ("custom.cellOptions", COLOR_TEXT)),
                   by_name("Received", ("unit", "dateTimeAsLocal")),
                   by_name("Processing (s)", ("unit", "s"), ("decimals", 1)),
                   by_name("Stage", ("custom.width", 220)),
                   by_name("Claim", ("custom.width", 100)),
                   by_regex("tokens$", ("unit", "locale"), ("decimals", 0)),
                   by_regex("cost$", ("unit", "currencyUSD"), ("decimals", 6)),
                   by_name("Prompt injection tokens", ("custom.width", 160)),
                   by_name("Summary tokens", ("custom.width", 130)),
                   by_name("Total tokens", ("custom.width", 120)),
                   by_name("Prompt injection AI cost", ("custom.width", 180)),
                   by_name("Summary AI cost", ("custom.width", 150)),
                   by_name("Total AI cost", ("custom.width", 140))]), 24)], h=16)
    L.place([
        (bars("Sorted by claim type", CLAIMS + f"""SELECT {CLASS_LABEL.format(b='decision')} AS classification, count(*) AS claims
FROM claims WHERE decision IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1""",
              "Loss type the automation assigned to each claim.", "classification", color="blue"), 12),
        (bars("Decisions by reviewer", CLAIMS + """SELECT reviewer,
  count(*) FILTER (WHERE review_status = 'approved') AS "Accepted",
  count(*) FILTER (WHERE review_status = 'rejected') AS "Rejected"
FROM claims
WHERE review_status IN ('approved', 'rejected')
GROUP BY reviewer
ORDER BY count(*) DESC, reviewer""", "Reviewer workload and outcomes: accept and reject decisions per person.",
              "reviewer", stacked=True, legend=True,
              overrides=[by_name("Accepted", ("color", {"mode": "fixed", "fixedColor": "green"})),
                         by_name("Rejected", ("color", {"mode": "fixed", "fixedColor": "red"}))]), 12),
    ], h=8)

    L.row("Gold check (feedback/dev_gold.json)")
    check_cols = ",\n  ".join(f"CASE WHEN NOT drafted THEN '-' WHEN {k} THEN 'Pass' ELSE 'Fail' END AS \"{label}\"" for k, label in GOLD_CHECKS)
    failures = bars("Failures by check", GOLD_BASE + "SELECT label AS check_name, failed FROM (\n  "
                    + "\n  UNION ALL ".join(f"SELECT {i} AS ord, '{label}' AS label, count(*) FILTER (WHERE drafted AND NOT {k}) AS failed FROM results"
                                            for i, (k, label) in enumerate(GOLD_CHECKS, 1))
                    + "\n) t ORDER BY ord", "How many benchmark claims failed each individual gold check. Zero everywhere is the target.",
                    "check_name", color="red", orientation="vertical")
    L.place([(failures, 24)], h=7)
    pass_fail = color_map([("Pass", "green"), ("Fail", "red"), ("No draft yet", "orange"), ("-", "text")])
    L.place([(table("Gold check by claim", GOLD_BASE + f"""SELECT case_id AS "Claim",
  CASE WHEN NOT drafted THEN 'No draft yet' WHEN passed THEN 'Pass' ELSE 'Fail' END AS "Result",
  {check_cols}
FROM results
ORDER BY passed, case_id""", "Each benchmark claim's latest draft scored against its gold answer. Failed claims are listed first.",
        overrides=[by_regex("^(Result|" + "|".join(label for _, label in GOLD_CHECKS) + ")$",
                            ("mappings", pass_fail), ("custom.cellOptions", COLOR_TEXT), ("custom.align", "center")),
                   by_name("Claim", ("custom.width", 100))]), 24)], h=14)

    L.row("Gemma model: tokens and cost")
    call_split = """count(*) FILTER (WHERE step = '3') AS "Prompt injection",
  count(*) FILTER (WHERE step = '6') AS "Summary", count(*) AS "Total" """
    L.place([
        (stat("Input tokens", f"SELECT {split_select(input_tokens)} {MODEL_FROM}",
              "Tokens sent to Gemma 4 31B. Prompt injection is the step 3 screen, summary is step 6, and total is the two combined.",
              "blue", unit="locale", split=True), 6),
        (stat("Output tokens", f"SELECT {split_select(output_tokens)} {MODEL_FROM}",
              "Tokens generated by Gemma 4 31B, split the same way as input tokens.",
              "blue", unit="locale", split=True), 6),
        (stat("Tokens per claim", f"SELECT {split_rounded(tokens_per_claim, 0)} {MODEL_FROM}",
              "Average input plus output tokens per claim. Prompt injection, summary, and the combined total.",
              "blue", unit="locale", split=True), 6),
        (stat("Model calls", f"SELECT {call_split} {MODEL_FROM}",
              "Gemma invocations. One prompt-injection screen and one summary per claim, plus any retries.",
              "blue", split=True), 6),
    ], h=8)
    L.place([
        (bars("Tokens by model step", f"""SELECT CASE step WHEN '3' THEN 'Prompt injection' ELSE 'Summary' END AS step_name,
  sum(input_tokens) AS "Input tokens", sum(output_tokens) AS "Output tokens"
FROM trace.trace_events WHERE {MODEL_ROWS} AND {TRACE_TIME}
GROUP BY step ORDER BY step""", "Input and output tokens for the prompt-injection screen and for summary generation.",
              "step_name", legend=True), 12),
        (bars("AI cost by claim", f"""SELECT case_id,
  round({cost_expr(INJECTION_ROWS)}::numeric, 6) AS "Prompt injection",
  round({cost_expr(SUMMARY_ROWS)}::numeric, 6) AS "Summary"
FROM trace.trace_events WHERE {MODEL_ROWS} AND {TRACE_TIME}
GROUP BY case_id ORDER BY case_id""",
              "Model cost for each claim, split between the prompt-injection screen and summary generation. Stacked height is the total.",
              "case_id", unit="currencyUSD", orientation="vertical", rotate=-45, stacked=True, legend=True,
              overrides=[by_name("Prompt injection", ("color", {"mode": "fixed", "fixedColor": "orange"})),
                         by_name("Summary", ("color", {"mode": "fixed", "fixedColor": "purple"}))]), 12),
    ], h=8)

    L.row("Latency")
    L.place([
        (stat("Median Gemma call", f"""SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::int AS ms
FROM trace.trace_events WHERE {MODEL_ROWS} AND duration_ms IS NOT NULL AND {TRACE_TIME}""",
              "Typical time for one Gemma call (injection screen or claim summary).", "blue", unit="ms"), 4),
        (bars("Step latency, median and p95", f"""SELECT {STEP_LABEL} AS step_name,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::int AS "Median",
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS "p95"
FROM trace.trace_events
WHERE duration_ms IS NOT NULL AND {TRACE_TIME}
GROUP BY step ORDER BY step::int""", "Time each pipeline step takes. The gap between median and p95 shows how unpredictable a step is.",
              "step_name", unit="ms", legend=True), 10),
        (bars("Processing time by claim", f"""SELECT case_id, round((sum(duration_ms) / 1000.0)::numeric, 1) AS seconds
FROM trace.trace_events
WHERE {PROCESSING_ROWS} AND {TRACE_TIME}
GROUP BY case_id ORDER BY case_id""", "Automated work per claim, intake to review request.",
              "case_id", unit="s", orientation="vertical", rotate=-45), 10),
    ], h=10)
    p95 = stat("p95 processing per claim", f"""SELECT round(percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds)::numeric, 1) AS seconds FROM (
  SELECT case_id, sum(duration_ms) / 1000.0 AS seconds
  FROM trace.trace_events
  WHERE {PROCESSING_ROWS} AND {TRACE_TIME}
  GROUP BY case_id
) per_claim""", "95% of claims finish their automated work within this time.", "blue", unit="s", decimals=1)
    L.panels[-3]["gridPos"]["h"] = 5
    p95["gridPos"] = {"h": 5, "w": 4, "x": 0, "y": L.y - 5}
    L.add(p95)

    L.row("Reliability")
    L.place([
        (stat("Failed steps", f"SELECT count(*) FILTER (WHERE status = 'error') AS errors FROM trace.trace_events WHERE {TRACE_TIME}",
              "Pipeline steps that ended in error, including ones later retried.", "red"), 6),
        (stat("Upstream timeouts", f"SELECT count(*) FILTER (WHERE error_code = 'upstream_timeout') AS timeouts FROM trace.trace_events WHERE {TRACE_TIME}",
              "Calls to the policy, claims or reviewer services that timed out.", "orange"), 6),
        (stat("Retried steps", f"SELECT count(*) FILTER (WHERE attempt > 1) AS retries FROM trace.trace_events WHERE {TRACE_TIME}",
              "Step attempts after the first one.", "orange"), 6),
        (stat("Audit chain breaks", """SELECT count(*) AS breaks FROM (
  SELECT prev_hash, lag(event_hash) OVER (ORDER BY seq) AS expected FROM trace.trace_events
) t WHERE expected IS NOT NULL AND prev_hash IS DISTINCT FROM expected""",
              "Tamper check over the whole trace log (all time). Anything above zero needs investigation.",
              thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]), 6),
    ], h=4)
    L.place([
        (bars("Error rate by step", f"""SELECT {STEP_LABEL} AS step_name,
  round(100.0 * count(*) FILTER (WHERE status = 'error') / count(*), 1) AS error_pct
FROM trace.trace_events
WHERE {TRACE_TIME}
GROUP BY step ORDER BY step::int""", "Percentage of attempts at each step that ended in error.",
              "step_name", color="red", unit="percent"), 12),
        (table("Latest failed steps", f"""SELECT started_at::timestamptz AS "Time", case_id AS "Claim", {STEP_LABEL} AS "Step",
  error_code AS "Error", attempt AS "Attempt", duration_ms AS "Duration"
FROM trace.trace_events
WHERE status = 'error' AND {TRACE_TIME}
ORDER BY seq DESC
LIMIT 50""", "Most recent pipeline errors, newest first.",
               overrides=[by_name("Time", ("unit", "dateTimeAsLocal"), ("custom.width", 170)),
                          by_name("Duration", ("unit", "ms")), by_name("Attempt", ("custom.width", 80))]), 12),
    ], h=8)

    L.row("AWS infrastructure")

    def ts(title, query, unit, description):
        return {"type": "timeseries", "title": title, "description": description, "datasource": CW,
                "fieldConfig": {"defaults": {"unit": unit, "min": 0, "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
                                                                                   "spanNulls": True}},
                                "overrides": []},
                "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                            "tooltip": {"mode": "multi", "sort": "desc"}},
                "targets": [query]}

    L.place([
        (ts("ECS CPU by service", cloudwatch("AWS/ECS", "CPUUtilization", {"ClusterName": "fnol", "ServiceName": "*"}, "Average",
                                             "${PROP('Dim.ServiceName')}"), "percent", "CPU use of each FNOL service on ECS."), 6),
        (ts("ECS memory by service", cloudwatch("AWS/ECS", "MemoryUtilization", {"ClusterName": "fnol", "ServiceName": "*"}, "Average",
                                                "${PROP('Dim.ServiceName')}"), "percent", "Memory use of each FNOL service on ECS."), 6),
        (ts("Database connections", cloudwatch("AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": "fnol-postgres"}, "Maximum",
                                               "connections"), "short", "Open connections on fnol-postgres."), 6),
        (ts("Database CPU", cloudwatch("AWS/RDS", "CPUUtilization", {"DBInstanceIdentifier": "fnol-postgres"}, "Average", "cpu"),
            "percent", "CPU use on fnol-postgres."), 6),
    ], h=7)
    groups = ["intake", "policy", "rules", "citations", "enterprise", "reviewer", "analyst"]
    L.place([({"type": "logs", "title": "Service errors and tracebacks",
               "description": "Error, exception and traceback lines from all seven FNOL services (CloudWatch Logs).",
               "datasource": CW,
               "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True,
                           "dedupStrategy": "none", "prettifyLogMessage": False, "showCommonLabels": False, "showLabels": False},
               "targets": [{"refId": "A", "datasource": CW, "queryMode": "Logs", "region": "default", "id": "",
                            "expression": "fields @timestamp, @log, @message\n| filter @message like /(?i)(error|exception|traceback)/\n| sort @timestamp desc\n| limit 200",
                            "logGroups": [{"arn": f"arn:aws:logs:us-east-1:104005345381:log-group:/fnol/{g}:*", "name": f"/fnol/{g}"} for g in groups],
                            "statsGroups": []}]}, 24)], h=10)

    return dashboard("fnol-ops", "FNOL Operations & AI Performance",
                     "Team drill-down behind the executive summary: per-claim detail, gold-check failures, Gemma token use, "
                     "latency, errors and AWS health. Headline totals live only on the executive summary.",
                     L, [("Executive summary", "/d/fnol-executive")], hide_vars=False)


if __name__ == "__main__":
    for name, board in (("fnol-executive.json", executive()), ("fnol-operations.json", operations())):
        (OUT / name).write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {OUT / name} ({len(board['panels'])} panels)")
