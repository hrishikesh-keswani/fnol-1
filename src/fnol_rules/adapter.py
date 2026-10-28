"""Map intake Input DB revisions into Rules structured fields."""
from __future__ import annotations

from fnol_rules.types import SourceFacts, StructuredFields


def _optional_bool(value):
    if isinstance(value, bool):
        return value
    return None


def fields_from_payload(payload: dict) -> StructuredFields:
    """Build StructuredFields from an intake revision payload (no raw_text used for routing)."""
    loss_type = payload.get("loss_type") or "unsupported"
    injury = _optional_bool(payload.get("injury_reported"))
    drivable = _optional_bool(payload.get("vehicle_drivable"))
    repeated = payload.get("repeated_fields") or ()

    intake = SourceFacts(
        loss_type=payload.get("loss_type"),
        vehicle_id=payload.get("vehicle_id"),
        incident_timestamp=payload.get("incident_timestamp"),
        driver_name=payload.get("driver_name"),
        incident_location=payload.get("incident_location"),
        injury_reported=injury,
        vehicle_drivable=drivable,
        referral_indicator=payload.get("referral_indicator"),
    )
    report = None
    if any(
        payload.get(k) is not None
        for k in ("report_vehicle_reference", "report_incident_timestamp", "reported_loss_type")
    ):
        report = SourceFacts(
            loss_type=payload.get("reported_loss_type"),
            vehicle_id=payload.get("report_vehicle_reference"),
            incident_timestamp=payload.get("report_incident_timestamp"),
        )
    estimate = None
    if payload.get("estimate_vehicle") is not None:
        estimate = SourceFacts(vehicle_id=payload.get("estimate_vehicle"))

    return StructuredFields(
        loss_type=loss_type,
        vehicle_id=payload.get("vehicle_id"),
        incident_timestamp=payload.get("incident_timestamp"),
        driver_name=payload.get("driver_name"),
        incident_location=payload.get("incident_location"),
        injury_reported=injury,
        vehicle_drivable=drivable,
        referral_indicator=payload.get("referral_indicator"),
        repeated_fields=tuple(repeated),
        intake=intake,
        report=report,
        estimate=estimate,
        injection_field_status=payload.get("injection_field_status"),
        injection_model_status=payload.get("injection_model_status"),
        extra_fields=tuple(
            {"source_id": item.get("source_id"), "label": item.get("label")}
            for item in payload.get("extra_fields") or ()
            if isinstance(item, dict)
        ),
    )
