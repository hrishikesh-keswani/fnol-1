"""Parse supplied FNOL markdown into structured fields. The original text stays intact."""

import hashlib
from pathlib import Path

NOT_SUPPLIED = "not supplied"

INTAKE_LABELS = {
    "Incident timestamp": "incident_timestamp",
    "Policy number": "policy_number",
    "Vehicle ID": "vehicle_id",
    "Loss type": "loss_type",
    "Driver name": "driver_name",
    "Incident location": "incident_location",
    "Injury reported": "injury_reported_raw",
    "Vehicle drivable": "vehicle_drivable_raw",
    "Referral indicator": "referral_indicator",
    "Loss reference": "loss_reference",
}

ESTIMATE_LABELS = {
    "Estimate reference": "estimate_reference",
    "Vehicle": "vehicle",
    "Repair amount": "repair_amount",
    "Prepared by": "prepared_by",
}

REPORT_LABELS = {
    "Vehicle reference": "vehicle_reference",
    "Incident timestamp": "incident_timestamp",
    "Reported loss type": "reported_loss_type",
}


def file_sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_source(directory, source_id):
    """Return (path, bytes, text) for a source file inside directory, or None.

    Uses S3 or Azure Blob when configured; otherwise reads the local disk.
    """
    from fnol_foundation.blobs import read_source as _read

    return _read(directory, source_id)


def parse_document(text, labels):
    """Split one markdown file into prose, known fields, and leftover lines.

    Known labels are the only structured fields. Any other line, including a
    hostile instruction, stays in unlabeled_lines and in the caller's raw text.
    A repeated known label keeps the first value.
    """
    account = []
    fields = {}
    duplicate_fields = []
    unlabeled = []
    started = False
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("Source ID:"):
            continue
        key, sep, value = line.partition(": ")
        if sep and key in labels:
            started = True
            name = labels[key]
            if name in fields:
                duplicate_fields.append(name)
            else:
                fields[name] = value
        elif started:
            unlabeled.append(line)
        else:
            account.append(line)
    return {
        "account_text": "\n".join(account).strip(),
        "fields": fields,
        "duplicate_fields": duplicate_fields,
        "unlabeled_lines": unlabeled,
    }


def document_kind(source_id, intake_source):
    if source_id == intake_source or source_id.endswith("-INTAKE"):
        return "intake"
    if source_id.endswith("-ESTIMATE"):
        return "estimate"
    if source_id.endswith("-REPORT"):
        return "report"
    return "unknown"


def labels_for(kind):
    if kind == "intake":
        return INTAKE_LABELS
    if kind == "estimate":
        return ESTIMATE_LABELS
    if kind == "report":
        return REPORT_LABELS
    return {}


def yes_no(raw):
    if raw is None:
        return None, None
    lowered = raw.strip().lower()
    if lowered == "yes":
        return raw, True
    if lowered == "no":
        return raw, False
    return raw, None


def supplied_value(raw):
    """Return (stored text or None, supplied). 'not supplied' is present but not usable."""
    if raw is None or not str(raw).strip():
        return None, False
    if str(raw).strip().lower() == NOT_SUPPLIED:
        return str(raw).strip(), False
    return str(raw).strip(), True


def load_procedures(policies_dir):
    """Read each policy markdown once and keep its source id and sha256.

    The stored record is the id and hash. Policy wording is not copied into the
    revision because the intake procedure names the restricted field marker.
    Uses S3 when FNOL_S3_BUCKET is set.
    """
    from fnol_foundation.blobs import list_markdown_sources

    return list_markdown_sources(policies_dir)
