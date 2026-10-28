"""Participant fixture integrity check (does not test a participant solution)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(data):
    dataset = json.loads((data / "dataset.json").read_text())
    ids = [x["case_id"] for x in dataset["cases"]]
    assert len(ids) == len(set(ids)), "Duplicate case IDs"
    for case in dataset["cases"]:
        assert case["intake_source"] in case["documents"]
        for sid in case["documents"]:
            assert (data / "documents" / (sid + ".md")).read_text().strip() == dataset["sources"][sid].strip(), sid
        for payload in case["tools"].values():
            assert json.dumps(payload, sort_keys=True) == dataset["sources"][payload["source_id"]]
        assert all(k in case["tools"] for k in case.get("faults", {}))
    for p in (data / "policies").glob("*.md"):
        assert p.read_text().strip() == dataset["sources"][p.stem].strip(), p
    print(f"Fixture integrity passed: {len(ids)} cases; {len(dataset['sources'])} sources.")


if __name__ == "__main__":
    validate(ROOT / "data/dev")
