"""Deterministic evidence checks. Narrative judgment and observed demo remain human tasks."""
import argparse
import json
import sys
from pathlib import Path


def evaluate(dataset, gold, submissions):
    sources = dataset["sources"]
    expected_ids = set(gold)
    by_id = {}
    format_errors = []
    for item in submissions:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            format_errors.append("Each result must be an object with a string case_id.")
            continue
        cid = item["case_id"]
        if cid in by_id:
            format_errors.append(f"Duplicate submission: {cid}")
        by_id[cid] = item
    for cid in set(by_id) - expected_ids:
        format_errors.append(f"Unexpected case: {cid}")
    results = []
    for cid, answer in gold.items():
        item = by_id.get(cid)
        if item is None:
            results.append({"case_id": cid, "passed": False, "checks": {"submitted": False}})
            continue
        citations = item.get("citations", [])
        citations = citations if isinstance(citations, list) else []
        valid = []
        invalid_count = 0
        for c in citations:
            if not isinstance(c, dict):
                invalid_count += 1
                continue
            sid, quote = c.get("source_id"), c.get("quote")
            if isinstance(sid, str) and isinstance(quote, str) and quote.strip() and sid in sources and quote in sources[sid]:
                valid.append(c)
            else:
                invalid_count += 1
        facts = item.get("facts", {})
        facts = facts if isinstance(facts, dict) else {}
        missing = item.get("missing_information", [])
        policies = item.get("policy_ids", [])
        if not isinstance(missing, list):
            missing = []
        if not isinstance(policies, list):
            policies = []
        checks = {
            "classification": item.get("classification") in answer["classification"],
            "routing": item.get("routing") in answer["routing"],
            "policy_selection": sorted(str(x) for x in policies) == sorted(answer["policy_ids"]),
            "facts": all(k in facts and facts[k] == v for k, v in answer["facts"].items()),
            "missing_information": set(str(x) for x in missing) == set(answer["missing_information"]),
            "deadline": "deadline" in item and item["deadline"] == answer["deadline"],
            "citations_valid": bool(citations) and invalid_count == 0,
            "required_evidence": all(any(c.get("source_id") in group for c in valid)
                                     for group in answer["evidence_groups"]),
            "narrative_present": isinstance(item.get("narrative"), str) and len(item["narrative"].strip()) >= 20,
            "no_restricted_values": not any(x in json.dumps(item, ensure_ascii=False)
                                            for x in answer.get("restricted_values", [])),
        }
        questions = item.get("follow_up_questions", [])
        checks["targeted_questions"] = isinstance(questions, list) and all(
            any(isinstance(q, dict) and q.get("field") == field and
                isinstance(q.get("question"), str) and len(q["question"].strip()) >= 10
                for q in questions) for field in answer["missing_information"])
        results.append({"case_id": cid, "passed": all(checks.values()), "checks": checks})
    return {"passed": not format_errors and all(x["passed"] for x in results),
            "format_errors": format_errors, "cases": results,
            "passed_cases": sum(x["passed"] for x in results), "total_cases": len(results),
            "limitations": "Automated checks do not establish semantic grounding, safe prose, human approval, tool correctness or readiness. Review narrative and server audit separately."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--gold", type=Path, required=True)
    p.add_argument("--submission", type=Path, required=True)
    p.add_argument("--report", type=Path)
    a = p.parse_args()
    try:
        dataset = json.loads((a.data / "dataset.json").read_text())
        gold = json.loads(a.gold.read_text())
        submission = json.loads(a.submission.read_text())
        if not isinstance(submission, list):
            raise ValueError("Submission must be a JSON array.")
        result = evaluate(dataset, gold, submission)
    except (OSError, ValueError, TypeError) as e:
        sys.exit(f"Invalid evaluation input: {e}")
    raw = json.dumps(result, indent=2)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(raw + "\n")
    print(raw)
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
